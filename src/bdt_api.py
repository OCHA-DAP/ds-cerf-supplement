"""Client for the public Beneficiary Data Tool (BDT2) — the CBPF *deduplicated*
people figures (``https://pfbi-eastus2-api-site.azurewebsites.net/bdt2/api/public/v1/``).

OneGMS project rows count a person once per project; the BDT applies a
deduplication scenario across projects/allocations, so its totals are a fund's
headline reach. The two never reconcile and are stored separately.

Routes (anonymous, read-only; ``$format=csv`` for tabular, ``/templates/`` is JSON):

* ``/beneficiary/`` — ``year=<yyyy>`` + ``only_allocation=1`` +
  ``allocation_category=ALL|US`` gives one row per fund × allocation
  (× location with ``isByLocation=true``); ``year`` alone gives the fund-level
  deduplicated total; ``template_name=`` / ``group_name=`` give a stored template's
  figure (**do not** pass ``year`` with those — it empties the feed).
* ``/beneficiaryByDisabilities/`` — same parameters, people with disabilities.
* ``/templates/`` — the template registry (which allocations each template dedups
  over); ``AllocationTypeIds`` / ``GroupNames`` are list-valued.

The legacy v2 feed (``/beneficiary/api/v2/``) returned no rows for 2025 on
2026-09-18 and is not mirrored.
"""
from __future__ import annotations

import csv
import io
import json

import requests

BASE = "https://pfbi-eastus2-api-site.azurewebsites.net/bdt2/api/public/v1"
_TIMEOUT = 180
_session = requests.Session()


def get(route: str, **params) -> list[dict]:
    """Rows from a BDT route. ``None`` params are dropped (BDT dislikes blanks)."""
    q = {k: v for k, v in params.items() if v is not None}
    is_json = route.strip("/") == "templates"
    q["$format"] = "json" if is_json else "csv"
    resp = _session.get(f"{BASE}/{route.strip('/')}/", params=q, timeout=_TIMEOUT)
    resp.raise_for_status()
    if is_json:
        data = resp.json()
        rows = data.get("data", data) if isinstance(data, dict) else data
        # list-valued cells → JSON strings; the loader types them as JSON
        return [{k: (v if not isinstance(v, (list, dict)) else json.dumps(v)) for k, v in r.items()}
                for r in rows]
    text = resp.text
    if text.lstrip().startswith("<"):
        raise RuntimeError(f"BDT returned HTML for {resp.url}")
    return list(csv.DictReader(io.StringIO(text)))


def template_names() -> list[str]:
    return [t["TemplateName"] for t in get("templates") if t.get("TemplateName")]


def group_names() -> list[str]:
    """Distinct group names across templates (``US_Tranche1_2026`` …). A group is its
    own deduplication query — never the sum of its templates."""
    return sorted({g for t in get("templates") for g in json.loads(t.get("GroupNames") or "[]")})
