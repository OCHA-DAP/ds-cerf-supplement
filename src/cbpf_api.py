"""Client for the public CBPF / OneGMS OData API (cbpfapi.unocha.org).

Three public surfaces, no auth:

* **OData entity sets** — ``/vo3/odata/<EntitySet>`` (28 sets; the ``$metadata``
  document types every column) and the older ``/vo1/odata/<EntitySet>`` (50 sets,
  still live, and the only public home of budget lines, logframes, financial reports,
  contributions, milestones, scorecards, partner due diligence…). Some accept a
  ``poolfundAbbrv=<PFAbbrv>`` filter; the big per-project ones *require* it in
  practice (the unfiltered call times out).
* **Stored queries** — ``/vo3/odata/GlobalGenericDataExtract?SPCode=<CODE>&…``. 127
  are catalogued at ``/vo3/``; 33 are public, the rest answer on the public host with
  a one-row ``{"Error": "This is secure procedure…"}`` body (HTTP 200!). Most take
  ``PoolfundCodeAbbrv``; ``ShowAllPooledFunds=1`` widens some to the whole portfolio.
* **Beneficiary Data Tool (BDT)** — a separate host with *deduplicated* people
  figures (``src/bdt_api.py``).

Gotchas this module absorbs (see the KB page datasets/cbpf-odata.md):

* A missing/invalid parameter is an HTTP 500 **HTML page**; a secured SPCode is an
  HTTP 200 ``Error`` row. Both are raised as :class:`ApiError` here.
* Regional funds share one ``PFAbbrv`` across their child funds (``AP501`` = BGD,
  PAK, FJI, VUT, SLB): fetch per *distinct* abbrev, keep the row's own PooledFundId.
* Some French-locale text is double-encoded UTF-8 (``approuvÃ©``) — repaired with
  a Latin-1 round trip in :func:`fix_mojibake`.
* Intermittent stalls / IncompleteRead on large pulls — retried with backoff.
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
import xml.etree.ElementTree as ET
from functools import lru_cache

import requests

HOST = "https://cbpfapi.unocha.org"
VO3 = f"{HOST}/vo3/odata"
VO1 = f"{HOST}/vo1/odata"
GENERIC = f"{VO3}/GlobalGenericDataExtract"

_TIMEOUT = 300
_ATTEMPTS = 4
_BACKOFF = 20  # s, doubled each attempt

_EDM = "{http://schemas.microsoft.com/ado/2009/11/edm}"
_session = requests.Session()
_session.headers["Accept"] = "application/json"


class ApiError(RuntimeError):
    """The API answered, but not with data (HTML 500 page, secured-SPCode Error row…)."""


# ----------------------------------------------------------------------------- http
def _get(url: str, params: dict | None = None, timeout: int = _TIMEOUT) -> requests.Response:
    last: Exception | None = None
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            resp = _session.get(url, params=params, timeout=timeout)
            if resp.status_code >= 500:
                # 500 with an HTML body = bad/missing parameter, not transient
                body = resp.text[:200].lstrip()
                if body.startswith("<"):
                    raise ApiError(f"HTTP {resp.status_code} HTML error page for {resp.url}")
            resp.raise_for_status()
            return resp
        except ApiError:
            raise
        except (requests.RequestException, ConnectionError) as e:  # incl. IncompleteRead
            last = e
            wait = _BACKOFF * 2 ** (attempt - 1)
            print(f"  fetch attempt {attempt}/{_ATTEMPTS} failed ({type(e).__name__}: "
                  f"{str(e)[:120]}) — retry in {wait}s")
            if attempt < _ATTEMPTS:
                time.sleep(wait)
    raise last  # type: ignore[misc]


def _parse_rows(resp: requests.Response, fmt: str) -> list[dict]:
    text = resp.text
    if text.lstrip().startswith("<"):
        raise ApiError(f"HTML body (missing/invalid parameter?) for {resp.url}")
    if fmt == "csv":
        rows = list(csv.DictReader(io.StringIO(text)))
        if rows and list(rows[0].keys()) == ["Error"]:
            raise ApiError(rows[0]["Error"])
        return rows
    data = json.loads(text)
    if isinstance(data, dict) and "value" in data:
        rows = data["value"]
    elif isinstance(data, dict):
        # single-element sets (CBPFSummary?year=) come back as one bare object
        rows = [{k: v for k, v in data.items() if not k.startswith("odata.")}]
    else:
        rows = data
    if not isinstance(rows, list):
        raise ApiError(f"unexpected JSON shape for {resp.url}: {text[:120]}")
    if rows and isinstance(rows[0], dict) and list(rows[0].keys()) == ["Error"]:
        raise ApiError(rows[0]["Error"])
    return rows


# ----------------------------------------------------------------------- endpoints
def entity_set(name: str, version: str = "vo3", fund: str | None = None,
               fmt: str = "json", **query) -> list[dict]:
    """One OData collection, optionally filtered to a fund (``poolfundAbbrv``)."""
    base = VO3 if version == "vo3" else VO1
    params = {"$format": fmt}
    if fund:
        params["poolfundAbbrv"] = fund
    params.update(query)
    return _parse_rows(_get(f"{base}/{name}", params), fmt)


def stored_query(code: str, fund: str | None = None, fmt: str = "json",
                 **params) -> list[dict]:
    """One ``GlobalGenericDataExtract`` stored query.

    Pass parameters exactly as the catalogue spells them (``ShowAllPooledFunds``,
    ``AllocationYears`` …). A ``None`` value is sent as an empty string — the API
    wants every catalogued parameter *present*, blank if unused.
    """
    q = {"SPCode": code, "$format": fmt}
    if fund is not None:
        q["PoolfundCodeAbbrv"] = fund
    q.update({k: ("" if v is None else v) for k, v in params.items()})
    return _parse_rows(_get(GENERIC, q), fmt)


@lru_cache(maxsize=1)
def pooled_funds() -> list[dict]:
    """``MstPooledFund`` — the 46-row fund registry (regional envelopes + children)."""
    return entity_set("MstPooledFund")


def fund_abbrevs() -> list[str]:
    """Distinct ``PFAbbrv`` values (34): one fetch each covers regional children too."""
    return sorted({f["PFAbbrv"] for f in pooled_funds() if f.get("PFAbbrv")})


@lru_cache(maxsize=1)
def last_modified() -> str | None:
    """When OneGMS's reporting DB last refreshed — a free "anything changed?" check."""
    rows = entity_set("LastModified")
    return rows[0].get("last_updated_date") if rows else None


@lru_cache(maxsize=4)
def metadata_types(version: str = "vo3") -> dict[str, list[tuple[str, str]]]:
    """``$metadata`` → {EntitySetName: [(column, EdmType), …]} for typed DDL."""
    base = VO3 if version == "vo3" else VO1
    root = ET.fromstring(_get(f"{base}/$metadata").content)
    types = {t.get("Name"): t for t in root.iter(_EDM + "EntityType")}
    out = {}
    for es in root.iter(_EDM + "EntitySet"):
        t = types.get(es.get("EntityType").split(".")[-1])
        out[es.get("Name")] = [
            (p.get("Name"), p.get("Type").replace("Edm.", ""))
            for p in (t.findall(_EDM + "Property") if t is not None else [])
        ]
    return out


# --------------------------------------------------------------------------- utils
def fix_mojibake(s):
    """Repair double-encoded UTF-8 (``ANORÃ`` → ``ANORÍ``); correct text is unchanged."""
    if not isinstance(s, str) or not any(ord(c) > 127 for c in s):
        return s
    try:
        fixed = s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    return fixed


_SNAKE_1 = re.compile(r"(.)([A-Z][a-z]+)")
_SNAKE_2 = re.compile(r"([a-z0-9])([A-Z])")


def snake(name: str) -> str:
    """``CHFProjectCode`` → ``chf_project_code``; ``PFId`` → ``pf_id``; ``AdmLoc1`` → ``adm_loc1``."""
    s = _SNAKE_1.sub(r"\1_\2", name.replace(" ", "_").replace("-", "_"))
    s = _SNAKE_2.sub(r"\1_\2", s).lower()
    return re.sub(r"_+", "_", s).strip("_")
