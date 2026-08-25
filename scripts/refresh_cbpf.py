"""Refresh the OneGMS CBPF mirror tables from the public CBPF OData API.

CBPF/regional-fund allocations are the pooled-fund counterpart of `aa.cerf_allocation`:
one row per *allocation* (a titled Standard/Reserve envelope containing a set of
approved projects), keyed on `(PooledFundId, AllocationTypeId)` — AllocationTypeId
ALONE IS NOT UNIQUE (reused across funds, ~50 collisions; the CBPF cousin of the CERF
feed's ApplicationID gotcha). The same API also lists CERF
allocations (FundTypeId 2) — those are EXCLUDED here; `aa.cerf_allocation` (from the
CERF feed) stays authoritative for CERF.

Writes two pure mirror tables (this script is their SOLE writer):
- `aa.cbpf_allocation` — feed columns + the deterministic `aa_keyword` flag (same
  title/summary keyword convention as the CERF mirror)
- `aa.cbpf_fund` — the pooled-fund registry (MstPooledFund), incl. regional
  envelopes (RhPF-*) and their per-country children via `parent_pf_id`

and (re)creates `aa.v_allocation` — the fund-agnostic UNION view over the CERF and
CBPF allocation mirrors that downstream linking/reconciliation reads.

Auth: ocha-stratus get_engine(write=True); needs DSCI_AZ_DB_DEV_* (+ _WRITE) env and
PGSSLMODE=require. Run:  python scripts/refresh_cbpf.py [--dry-run]
"""
import argparse
import json
import os
import sys
import urllib.request

os.environ.setdefault("PGSSLMODE", "require")

API = "https://cbpfapi.unocha.org/vo2/odata"
SCHEMA = "aa"
AA_KEYWORDS = ("anticipat", "early action", "précoce", "precoce", "anticipatoire")
CBPF_FUND_TYPE_ID = 1  # FundTypeId 2 = CERF rows in the same feed — excluded

ALLOCATION_COLUMNS = [
    "allocation_type_id", "pooled_fund_id", "pooled_fund_name", "year",
    "allocation_source", "title", "summary", "planned_usd",
    "planned_start_date", "planned_end_date", "hrp_plans",
    "projects_under_approval", "under_approval_budget",
    "projects_approved", "approved_budget", "aa_keyword",
]
FUND_COLUMNS = [
    "pf_id", "name", "abbrv", "country_code_iso2", "managing_agent",
    "admin_agent", "is_public", "parent_pf_id",
]

DDL = f"""
create schema if not exists {SCHEMA};
create table if not exists {SCHEMA}.cbpf_allocation (
    allocation_type_id      int not null,
    pooled_fund_id          int not null,
    pooled_fund_name        text,
    year                    int,
    allocation_source       text,      -- Standard | Reserve
    title                   text,
    summary                 text,
    planned_usd             numeric,
    planned_start_date      date,
    planned_end_date        date,
    hrp_plans               text,
    projects_under_approval int,
    under_approval_budget   numeric,
    projects_approved       int,
    approved_budget         numeric,
    aa_keyword              boolean not null default false,
    updated_at              timestamptz not null default now(),
    primary key (pooled_fund_id, allocation_type_id)
);
create table if not exists {SCHEMA}.cbpf_fund (
    pf_id             int primary key,
    name              text not null,
    abbrv             text,
    country_code_iso2 text,
    managing_agent    text,
    admin_agent       text,
    is_public         boolean,
    parent_pf_id      int
);
"""

# fund-agnostic union over the allocation mirrors; is_aa = each mirror's keyword flag.
# amount: approved when present, else planned/requested (CBPF envelopes can be
# published before any project is approved).
V_ALLOCATION = f"""
create or replace view {SCHEMA}.v_allocation as
select 'cerf'::text                as fund_type,
       'CERF'::text                as fund_name,
       application_code            as allocation_code,
       country_iso3,
       year,
       coalesce(amount_approved, amount_requested) as amount_usd,
       aa_keyword                  as is_aa,
       title
from {SCHEMA}.cerf_allocation
union all
select case when a.pooled_fund_name ilike '%%rhpf%%'
              or a.pooled_fund_name ilike '%%regional%%'
            then 'regional_fund' else 'cbpf' end as fund_type,
       a.pooled_fund_name          as fund_name,
       'cbpf-' || a.pooled_fund_id::text || '-' || a.allocation_type_id::text
                                   as allocation_code,
       null::text                  as country_iso3,   -- via cbpf_fund.country_code_iso2
       a.year,
       coalesce(nullif(a.approved_budget, 0), a.planned_usd) as amount_usd,
       a.aa_keyword                as is_aa,
       a.title
from {SCHEMA}.cbpf_allocation a
"""


def _get(path):
    url = f"{API}/{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.load(resp)["value"]


def _date(s):
    return s[:10] if s and str(s)[:4].isdigit() else None


def fetch_allocations():
    rows = []
    for r in _get("AllocationTypes?$format=json"):
        if r.get("FundTypeId") != CBPF_FUND_TYPE_ID:
            continue
        # TITLE only: summaries mention "early action" in passing on huge generic
        # allocations (e.g. $62M "2020 1st Reserve Allocation") — title-only matches
        # the CERF mirror's precision
        hay = (r.get("AllocationTitle") or "").lower()
        rows.append(dict(
            allocation_type_id=r["AllocationTypeId"],
            pooled_fund_id=r.get("PooledFundId"),
            pooled_fund_name=r.get("PooledFundName"),
            year=r.get("AllocationYear"),
            allocation_source=r.get("AllocationSource"),
            title=r.get("AllocationTitle"),
            summary=r.get("AllocationSummary"),
            planned_usd=r.get("TotalUSDPlanned"),
            planned_start_date=_date(r.get("PlannedStartDate")),
            planned_end_date=_date(r.get("PlannedEndDate")),
            hrp_plans=r.get("HRPPlans"),
            projects_under_approval=r.get("TotalProjectsunderApproval"),
            under_approval_budget=r.get("TotalUnderApprovalBudget"),
            projects_approved=r.get("TotalProjectsApproved"),
            approved_budget=r.get("TotalApprovedBudget"),
            aa_keyword=any(k in hay for k in AA_KEYWORDS),
        ))
    ids = [(r["pooled_fund_id"], r["allocation_type_id"]) for r in rows]
    if len(ids) != len(set(ids)):
        sys.exit("(PooledFundId, AllocationTypeId) no longer unique in the feed — "
                 "investigate before loading.")
    return rows


def fetch_funds():
    return [dict(
        pf_id=r["PFId"], name=r.get("PFName"), abbrv=r.get("PFAbbrv"),
        country_code_iso2=r.get("PFCountryCode"), managing_agent=r.get("MAAgent"),
        admin_agent=r.get("AAgent"), is_public=r.get("IsPublic"),
        parent_pf_id=r.get("ParentPFId"),
    ) for r in _get("MstPooledFund?$format=json")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="fetch + report; no DB writes")
    args = ap.parse_args()

    allocations = fetch_allocations()
    funds = fetch_funds()
    n_aa = sum(r["aa_keyword"] for r in allocations)
    print(f"fetched {len(allocations)} CBPF/RhPF allocations ({n_aa} AA-keyword) "
          f"and {len(funds)} pooled funds from the CBPF OData API")
    if args.dry_run:
        return

    import ocha_stratus as stratus
    from sqlalchemy import text

    def upsert_sql(table, cols, key):
        keys = key.split(", ")
        setc = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in keys)
        return text(
            f"insert into {SCHEMA}.{table} ({', '.join(cols)}) "
            f"values ({', '.join(':' + c for c in cols)}) "
            f"on conflict ({key}) do update set {setc}"
        )

    eng = stratus.get_engine(stage="dev", write=True)
    with eng.begin() as c:
        for stmt in [s for s in DDL.split(";\n") if s.strip()]:
            c.execute(text(stmt))
        before = c.execute(text(f"select count(*) from {SCHEMA}.cbpf_allocation")).scalar()
        c.execute(upsert_sql("cbpf_allocation", ALLOCATION_COLUMNS,
                             "pooled_fund_id, allocation_type_id"),
                  allocations)
        c.execute(upsert_sql("cbpf_fund", FUND_COLUMNS, "pf_id"), funds)
        c.execute(text(V_ALLOCATION))
        after = c.execute(text(f"select count(*) from {SCHEMA}.cbpf_allocation")).scalar()
    print(f"cbpf mirror upserted: {len(allocations)} allocations · table {before} -> {after} rows; "
          f"{len(funds)} funds; view aa.v_allocation refreshed")


if __name__ == "__main__":
    main()
