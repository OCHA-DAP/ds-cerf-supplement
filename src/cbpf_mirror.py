"""Registry-driven raw mirror of the public CBPF API into the dev DB schema ``cbpf``.

Every public surface of cbpfapi.unocha.org (vo3 + vo1 entity sets, the 33 public
``GlobalGenericDataExtract`` stored queries) and the public Beneficiary Data Tool
routes is described once in :mod:`src.cbpf_registry` as a :class:`TableSpec`. This
module turns a spec into a table: fetch (fanning out per fund / per year where the
API demands it), type the columns (OData ``$metadata`` for entity sets, value
inference for the untyped CSV stored queries), snake_case the names, and
full-replace the table in one transaction. Column drift upstream is absorbed by
``ALTER TABLE … ADD COLUMN``; vanished columns simply stay NULL.

Every table carries ``fetched_at`` (the run's timestamp) and each run appends a row
per table to ``cbpf.mirror_run`` — the hook for the monthly snapshotting that is
planned to sit on top of this.

The AA-facing *normalized* CBPF tables in schema ``aa`` (``aa.cbpf_allocation``,
``aa.cbpf_project`` …, written by ``scripts/refresh_cbpf*.py``) are unchanged and
stay the tables downstream AA tracking reads; ``cbpf.*`` is the complete raw mirror.
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import sqlalchemy as sa
from sqlalchemy import text

from src import cbpf_api

SCHEMA = "cbpf"
_CHUNK = 2000

# ----------------------------------------------------------------------------- spec
FANOUT_NONE, FANOUT_FUND, FANOUT_YEAR, FANOUT_FUND_YEAR = None, "fund", "year", "fund_year"


@dataclass
class TableSpec:
    """One mirrored table.

    name     – table name in schema ``cbpf``
    kind     – ``es`` (OData entity set), ``sp`` (stored query), ``bdt`` (Beneficiary
               Data Tool; ``source`` is the route)
    source   – entity-set name / SPCode / BDT route
    version  – ``vo3`` | ``vo1`` (entity sets only)
    params   – extra query parameters, sent verbatim (``None`` → blank)
    fanout   – None | ``fund`` (one call per distinct PFAbbrv) | ``year`` |
               ``fund_year``; the fund/year is injected via ``fund_param`` /
               ``year_param``
    years    – years to iterate for year fan-outs (default 2014 → this year)
    key      – natural key (checked at load, recorded in mirror_run, NOT enforced —
               a raw mirror must load what the API serves)
    group    – ERD grouping: fund | allocation | project | partner | reporting |
               finance | master | people | bdt
    joins    – [(local_col, "other_table.other_col"), …] join-by-convention edges
               for the ERD (the API declares no foreign keys)
    desc     – one line for the ERD / docs
    """
    name: str
    kind: str
    source: str
    version: str = "vo3"
    params: dict = field(default_factory=dict)
    fanout: str | None = FANOUT_NONE
    fund_param: str = "PoolfundCodeAbbrv"
    year_param: str = "AllocationYear"
    years: list[int] | None = None
    param_fanout: tuple[str, list] | None = None   # e.g. ("InstanceTypeId", [1..7])
    key: tuple[str, ...] = ()
    group: str = "other"
    joins: list[tuple[str, str]] = field(default_factory=list)
    desc: str = ""
    fmt: str = "csv"        # stored queries: CSV has more columns than JSON (!)
    dedupe: bool = True     # drop exact-duplicate rows across fan-out calls
    expected_min_rows: int = 1   # an empty answer never replaces a populated table

    def year_range(self) -> list[int]:
        return self.years or list(range(2014, date.today().year + 1))


# --------------------------------------------------------------------------- fetch
def _call(spec: TableSpec, fund: str | None, year: int | None, extra: dict | None = None) -> list[dict]:
    params = dict(spec.params)
    if year is not None:
        params[spec.year_param] = year
    if extra:
        params.update(extra)
    if spec.kind == "es":
        if fund is not None:
            params["poolfundAbbrv"] = fund
        return cbpf_api.entity_set(spec.source, spec.version, fmt="json", **params)
    if spec.kind == "sp":
        return cbpf_api.stored_query(spec.source, fund=fund, fmt=spec.fmt, **params)
    if spec.kind == "bdt":
        import requests

        from src import bdt_api
        try:
            return bdt_api.get(spec.source, **params)
        except requests.HTTPError as e:
            # e.g. the GT_* global-scenario templates 404 on /beneficiary/ — a
            # per-call miss, tolerated like a per-fund HTML 500 on OneGMS
            raise cbpf_api.ApiError(str(e)) from e
    raise ValueError(spec.kind)


def fetch(spec: TableSpec, funds: list[str] | None = None, verbose: bool = True) -> tuple[list[dict], int]:
    """All rows for a spec (fan-out applied). Returns (rows, n_requests)."""
    funds = funds if funds is not None else cbpf_api.fund_abbrevs()
    calls: list[tuple[str | None, int | None]]
    if spec.fanout == FANOUT_FUND:
        calls = [(f, None) for f in funds]
    elif spec.fanout == FANOUT_YEAR:
        calls = [(None, y) for y in spec.year_range()]
    elif spec.fanout == FANOUT_FUND_YEAR:
        calls = [(f, y) for f in funds for y in spec.year_range()]
    else:
        calls = [(None, None)]
    extras: list[dict | None] = [None]
    if spec.param_fanout:
        pname, pvals = spec.param_fanout
        if callable(pvals):          # e.g. BDT template names, looked up at run time
            pvals = pvals()
        extras = [{pname: v} for v in pvals]
    calls = [(f, y, e) for f, y in calls for e in extras]
    rows: list[dict] = []
    failed = 0
    for i, (fund, year, extra) in enumerate(calls, 1):
        try:
            got = _call(spec, fund, year, extra)
        except cbpf_api.ApiError as e:
            # per-fund/year HTML 500s happen (a fund with no data for a query) —
            # tolerate a few, but a fully failing table is an error, not zero rows
            failed += 1
            if verbose:
                print(f"    {spec.name} [{fund or '-'} {year or ''}] api error: {str(e)[:100]}")
            continue
        rows.extend(got)
        if verbose and len(calls) > 1 and (i % 10 == 0 or i == len(calls)):
            print(f"    {spec.name}: {i}/{len(calls)} calls, {len(rows)} rows", flush=True)
    if failed == len(calls):
        raise cbpf_api.ApiError(f"{spec.name}: every call failed ({failed})")
    dropped = 0
    if spec.dedupe:   # exact duplicates carry no information (the feed itself has
                      # them: 121 legacy 2014–15 project codes twice in PF_PROJ_SUMMARY_V4)
        seen, out = set(), []
        for r in rows:
            k = json.dumps(r, sort_keys=True, default=str)
            if k not in seen:
                seen.add(k)
                out.append(r)
        dropped = len(rows) - len(out)
        rows = out
        if dropped and verbose:
            # vo1 per-fund calls return exact-duplicate rows (e.g. 24k of 108k on
            # ProjectSummaryWithLocationAndCluster) — recorded in mirror_run.note
            print(f"    {spec.name}: dropped {dropped} exact-duplicate rows")
    return rows, len(calls), dropped


# -------------------------------------------------------------------------- typing
_INT = re.compile(r"^-?\d{1,18}$")
_NUM = re.compile(r"^-?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?)?$")
_US = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}( \d{1,2}:\d{2}(:\d{2})? ?[AP]M)?$")
_BOOL = {"true", "false"}

_EDM_TO_SA = {
    "Int16": sa.Integer, "Int32": sa.Integer, "Int64": sa.BigInteger,
    "Double": sa.Float, "Single": sa.Float, "Decimal": sa.Numeric,
    "DateTime": sa.DateTime, "DateTimeOffset": sa.DateTime, "Boolean": sa.Boolean,
    "String": sa.Text, "Guid": sa.Text, "Byte": sa.Integer,
}


def _infer_type(values) -> type:
    """SQLAlchemy type for a column from its non-null values (CSV = all strings)."""
    kinds = set()
    for v in values:
        if v is None or v == "":
            continue
        if isinstance(v, bool):
            kinds.add("bool")
        elif isinstance(v, int):
            kinds.add("int")
        elif isinstance(v, float):
            kinds.add("num")
        elif isinstance(v, (dict, list)):
            kinds.add("json")
        else:
            s = str(v).strip()
            if _INT.match(s):
                kinds.add("int")
            elif _NUM.match(s):
                kinds.add("num")
            elif _ISO.match(s) or _US.match(s):
                kinds.add("ts")
            elif s.lower() in _BOOL:
                kinds.add("bool")
            else:
                return sa.Text
        if len(kinds) > 1 and kinds != {"int", "num"}:
            return sa.Text
    if not kinds:
        return sa.Text
    if kinds == {"json"}:
        return sa.JSON
    if kinds == {"bool"}:
        return sa.Boolean
    if kinds == {"ts"}:
        return sa.DateTime
    if kinds == {"int"}:
        return sa.BigInteger
    return sa.Numeric


def _coerce(v, sa_type):
    """Python value for a typed column (CSV strings → int/float/datetime/bool)."""
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None
    if sa_type is sa.JSON:
        return v
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    if sa_type in (sa.BigInteger, sa.Integer):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None
    if sa_type in (sa.Numeric, sa.Float):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    if sa_type is sa.Boolean:
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("true", "1")
    if sa_type is sa.DateTime:
        if isinstance(v, datetime):
            return v
        s = str(v).strip()
        for f in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
                  "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y"):
            try:
                return datetime.strptime(s, f)
            except ValueError:
                pass
        return None
    return cbpf_api.fix_mojibake(v) if isinstance(v, str) else str(v)


def build_columns(spec: TableSpec, rows: list[dict]) -> list[tuple[str, str, type]]:
    """[(api_name, snake_name, sa_type)] — metadata-typed for entity sets, inferred otherwise."""
    api_cols: list[str] = []
    for r in rows:
        for k in r.keys():
            if k not in api_cols:
                api_cols.append(k)
    typed: dict[str, type] = {}
    if spec.kind == "es":
        meta = dict(cbpf_api.metadata_types(spec.version).get(spec.source, []))
        for c in api_cols:
            edm = meta.get(c, "")
            if edm.startswith("Collection("):
                typed[c] = sa.JSON
            elif edm in _EDM_TO_SA:
                typed[c] = _EDM_TO_SA[edm]
    for c in api_cols:
        if c not in typed:
            typed[c] = _infer_type(r.get(c) for r in rows)
    out, seen = [], set()
    for c in api_cols:
        s = cbpf_api.snake(c)
        if s in seen:  # e.g. PooledFundId + PooledfundId both present
            s = s + "_2"
        seen.add(s)
        out.append((c, s, typed[c]))
    return out


# ---------------------------------------------------------------------------- load
RUN_DDL = f"""
create schema if not exists {SCHEMA};
create table if not exists {SCHEMA}.mirror_run (
    run_id       bigint generated always as identity primary key,
    table_name   text not null,
    kind         text not null,
    source       text not null,
    fetched_at   timestamptz not null,
    api_last_modified timestamptz,
    n_rows       int,
    n_requests   int,
    seconds      numeric,
    key_columns  text,
    key_unique   boolean,
    n_columns    int,
    note         text
);
create index if not exists mirror_run_table_idx on {SCHEMA}.mirror_run (table_name, fetched_at desc);
"""


def _ensure_table(conn, spec: TableSpec, cols) -> sa.Table:
    md = sa.MetaData(schema=SCHEMA)
    tbl = sa.Table(spec.name, md,
                   *[sa.Column(s, t) for _, s, t in cols],
                   sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False))
    existing = {r[0] for r in conn.execute(text(
        "select column_name from information_schema.columns "
        "where table_schema = :s and table_name = :t"), {"s": SCHEMA, "t": spec.name})}
    if not existing:
        tbl.create(conn)
        return tbl
    for _, s, t in cols:
        if s not in existing:
            ddl = sa.schema.CreateColumn(sa.Column(s, t)).compile(conn.engine)
            conn.execute(text(f"alter table {SCHEMA}.{spec.name} add column {ddl}"))
            print(f"    + new column {spec.name}.{s}")
    # columns that exist in the DB but not in this fetch are left alone (NULL)
    return tbl


def load(conn, spec: TableSpec, rows: list[dict], fetched_at: datetime, n_requests: int,
         seconds: float, api_last_modified: str | None = None, dropped: int = 0) -> dict:
    cols = build_columns(spec, rows)
    tbl = _ensure_table(conn, spec, cols)
    key = tuple(cbpf_api.snake(k) if k not in {s for _, s, _ in cols} else k for k in spec.key)
    typed_rows = [
        {s: _coerce(r.get(a), t) for a, s, t in cols} | {"fetched_at": fetched_at}
        for r in rows
    ]
    key_unique = None
    if key and typed_rows:
        keys = {tuple(r.get(k) for k in key) for r in typed_rows}
        key_unique = len(keys) == len(typed_rows)
    conn.execute(text(f"truncate {SCHEMA}.{spec.name}"))
    for i in range(0, len(typed_rows), _CHUNK):
        conn.execute(tbl.insert(), typed_rows[i:i + _CHUNK])
    conn.execute(text(f"""
        insert into {SCHEMA}.mirror_run (table_name, kind, source, fetched_at, api_last_modified,
            n_rows, n_requests, seconds, key_columns, key_unique, n_columns, note)
        values (:t, :k, :src, :fa, :lm, :n, :nr, :sec, :kc, :ku, :nc, :note)"""),
        dict(t=spec.name, k=spec.kind, src=f"{spec.version}:{spec.source}" if spec.kind == "es" else spec.source,
             fa=fetched_at, lm=api_last_modified, n=len(typed_rows), nr=n_requests,
             sec=round(seconds, 1), kc=",".join(key) or None, ku=key_unique, nc=len(cols),
             note="; ".join(filter(None, [
                 None if key_unique in (None, True) else "declared key not unique in feed",
                 f"{dropped} exact-duplicate rows dropped across calls" if dropped else None])) or None))
    return dict(rows=len(typed_rows), cols=len(cols), key_unique=key_unique)


def refresh(engine, specs: list[TableSpec], funds: list[str] | None = None,
            dry_run: bool = False, verbose: bool = True) -> list[dict]:
    """Fetch + full-replace each spec in its own transaction; one failure ≠ whole run."""
    results = []
    fetched_at = datetime.now(timezone.utc)
    lm = None
    try:
        lm = cbpf_api.last_modified()
    except Exception as e:  # noqa: BLE001
        print(f"LastModified unavailable: {e}")
    if not dry_run:
        with engine.begin() as c:
            for stmt in [s for s in RUN_DDL.split(";\n") if s.strip()]:
                c.execute(text(stmt))
    for spec in specs:
        t0 = time.time()
        print(f"{spec.name} ← {spec.kind}:{spec.source}" + (f" [{spec.fanout}]" if spec.fanout else ""), flush=True)
        try:
            rows, n_req, dropped = fetch(spec, funds=funds, verbose=verbose)
        except Exception as e:  # noqa: BLE001
            print(f"  FAILED fetch: {e}", file=sys.stderr)
            results.append(dict(table=spec.name, error=str(e)))
            continue
        if len(rows) < spec.expected_min_rows:
            print(f"  FAILED: {len(rows)} rows < expected minimum {spec.expected_min_rows} — "
                  f"not replacing the table", file=sys.stderr)
            results.append(dict(table=spec.name, error=f"only {len(rows)} rows"))
            continue
        if dry_run:
            cols = build_columns(spec, rows)
            print(f"  {len(rows)} rows, {len(cols)} cols, {n_req} requests, {time.time()-t0:.0f}s")
            results.append(dict(table=spec.name, rows=len(rows), cols=len(cols)))
            continue
        try:
            with engine.begin() as c:
                r = load(c, spec, rows, fetched_at, n_req, time.time() - t0, lm, dropped)
        except Exception as e:  # noqa: BLE001
            print(f"  FAILED load: {e}", file=sys.stderr)
            results.append(dict(table=spec.name, error=str(e)))
            continue
        print(f"  loaded {r['rows']} rows × {r['cols']} cols in {time.time()-t0:.0f}s"
              + ("" if r["key_unique"] in (None, True) else "  ⚠ declared key not unique"))
        results.append(dict(table=spec.name, **r))
        del rows
    return results
