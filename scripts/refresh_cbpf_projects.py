"""Refresh the CBPF project-level mirror from the public CBPF OData API.

A CBPF *project* is a grant to one specific implementing partner (NNGO/INGO/UN
agency/Red Cross) under an allocation — unlike CERF, where projects go to UN agencies
and NGOs appear only as subgrantees. The `ProjectSummary` endpoint returns one row per
project × cluster × admin-location; this script fetches per pooled fund (the
unfiltered endpoint times out) and normalizes into three tables (sole writer of all):

- `aa.cbpf_project`         — one row per project (org, type, budget, dates, status,
                              targets; `allocation_type_id` joins to
                              `aa.cbpf_allocation` on (pooled_fund_id, allocation_type_id))
- `aa.cbpf_project_cluster` — project × cluster budget split
- `aa.cbpf_project_subip`   — sub-implementing partners (the CBPF sub-grant layer)

Admin-location splits are deliberately NOT mirrored (the biggest expansion factor,
no AA-tracking consumer yet) — add later if needed.

Keyed on `chf_project_code` (globally unique — verified at load, exits if that ever
changes). ~44 API requests, a few minutes total. Full-replace load in one transaction
(chunked multi-row inserts — plain executemany is unusably slow against Azure).

If a fund's fetch still fails after the retries, or comes back with no projects while
some are stored for it, its previous rows are kept and every other fund is replaced
as usual. The script then exits non-zero naming the fund(s),
so the job's failure email fires (before 2026-10, the fund was silently dropped from
all three tables on a green run).

Auth: ocha-stratus get_engine(write=True); PGSSLMODE=require.
Run:  python scripts/refresh_cbpf_projects.py [--dry-run]
"""
import argparse
import json
import os
import sys
from pathlib import Path

import requests

# shared retrying fetch — the CBPF OData API 500s occasionally (2026-10-05)
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.cerf_api import _fetch_feed  # noqa: E402

os.environ.setdefault("PGSSLMODE", "require")

API = "https://cbpfapi.unocha.org/vo2/odata"
SCHEMA = "aa"

PROJECT_COLUMNS = [
    "chf_project_code", "pooled_fund_id", "pooled_fund_name", "allocation_type_id",
    "allocation_source", "allocation_year", "org_name", "org_type", "partner_code",
    "title", "project_status", "budget", "project_start_date", "project_end_date",
    "approved_date", "date_submitted", "men", "women", "boys", "girls",
]
CLUSTER_COLUMNS = [
    "chf_project_code", "cluster", "cluster_percentage", "budget_by_cluster",
]
SUBIP_COLUMNS = [
    "chf_project_code", "subip_name", "subip_type_id", "subip_amount",
]

DDL = f"""
create table if not exists {SCHEMA}.cbpf_project (
    chf_project_code   text primary key,
    pooled_fund_id     int,
    pooled_fund_name   text,
    allocation_type_id int,
    allocation_source  text,
    allocation_year    int,
    org_name           text,
    org_type           text,      -- UN Agency | International NGO | National NGO | Red Cross...
    partner_code       text,
    title              text,
    project_status     text,
    budget             numeric,
    project_start_date date,
    project_end_date   date,
    approved_date      date,
    date_submitted     date,
    men                bigint,
    women              bigint,
    boys               bigint,
    girls              bigint,
    updated_at         timestamptz not null default now()
);
create table if not exists {SCHEMA}.cbpf_project_cluster (
    chf_project_code  text not null,
    cluster           text not null,
    cluster_percentage numeric,
    budget_by_cluster numeric,
    unique (chf_project_code, cluster)
);
create index if not exists cbpf_project_cluster_code_idx
    on {SCHEMA}.cbpf_project_cluster (chf_project_code);
create table if not exists {SCHEMA}.cbpf_project_subip (
    chf_project_code text not null,
    subip_name       text not null,
    subip_type_id    int,
    subip_amount     numeric,
    unique (chf_project_code, subip_name, subip_amount)
);
create index if not exists cbpf_project_subip_code_idx
    on {SCHEMA}.cbpf_project_subip (chf_project_code);
"""


def _get(path):
    return json.loads(_fetch_feed(f"{API}/{path}", timeout=300))["value"]


def _date(s):
    return str(s)[:10] if s and str(s)[:4].isdigit() else None


def _num(v):
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(float(v)) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


def fetch_all():
    """Returns (projects, clusters, subips, failed, empty); `failed` and `empty` map
    the abbreviation of each fund whose fetch failed / came back with no projects
    to the pooled fund ids its rows carry."""
    # one abbreviation can cover several fund ids: the Asia-Pacific regional fund
    # (AP501) is listed once per country envelope, and its projects carry those
    # ids (94, 95, 97, 99, 505), not 501
    fund_ids = {}
    for f in _get("MstPooledFund?$format=json"):
        if f.get("PFAbbrv"):
            fund_ids.setdefault(f["PFAbbrv"], set()).add(f["PFId"])
    abbrvs = sorted(fund_ids)
    projects, clusters, subips = {}, {}, set()
    failed, empty = {}, {}
    for i, ab in enumerate(abbrvs, 1):
        try:
            rows = _get(f"ProjectSummary?poolfundAbbrv={ab}&$format=json")
        # _fetch_feed has already retried; a truncated body gets past
        # raise_for_status and fails here as bad JSON (ValueError)
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"  ERROR {ab}: fetch failed ({e}) — keeping its previous rows",
                  file=sys.stderr)
            failed[ab] = fund_ids[ab]
            continue
        print(f"  [{i}/{len(abbrvs)}] {ab}: {len(rows)} expanded rows")
        if not any(r.get("ChfProjectCode") for r in rows):
            empty[ab] = fund_ids[ab]
        for r in rows:
            code = r.get("ChfProjectCode")
            if not code:
                continue
            if code not in projects:
                projects[code] = dict(
                    chf_project_code=code,
                    pooled_fund_id=r.get("PooledFundId"),
                    pooled_fund_name=r.get("PooledFundName"),
                    allocation_type_id=r.get("AllocationTypeId"),
                    allocation_source=r.get("AllocationSourceName"),
                    allocation_year=r.get("AllocationYear"),
                    org_name=r.get("OrganizationName"),
                    org_type=r.get("OrganizationType"),
                    partner_code=r.get("PartnerCode"),
                    title=r.get("ProjectTitle"),
                    project_status=r.get("ProjectStatus"),
                    budget=_num(r.get("Budget")),
                    project_start_date=_date(r.get("ProjectStartDate")),
                    project_end_date=_date(r.get("ProjectEndDate")),
                    approved_date=_date(r.get("ApprovedDate")),
                    date_submitted=_date(r.get("DateSubmitted")),
                    men=r.get("Men"), women=r.get("Women"),
                    boys=r.get("Boys"), girls=r.get("Girls"),
                )
            cl = r.get("Cluster")
            if cl and (code, cl) not in clusters:
                clusters[(code, cl)] = dict(
                    chf_project_code=code, cluster=cl,
                    cluster_percentage=_num(r.get("ClusterPercentage")),
                    budget_by_cluster=_num(r.get("BudgetByCluster")),
                )
            # SubIP fields are ##-delimited parallel lists (several sub-partners
            # crammed into one feed cell) — explode them
            sub = r.get("SubIPName")
            if sub:
                names = [n.strip() for n in str(sub).split("##")]
                types = str(r.get("SubIPTypeId") or "").split("##")
                amts = str(r.get("SubIPAmt") or "").split("##")
                for j, nm in enumerate(names):
                    if not nm:
                        continue
                    subips.add((
                        code, nm,
                        _int(types[j]) if j < len(types) else None,
                        _num(amts[j]) if j < len(amts) else None,
                    ))
    subip_rows = [dict(zip(SUBIP_COLUMNS, s)) for s in sorted(subips, key=str)]
    return list(projects.values()), list(clusters.values()), subip_rows, failed, empty


def stored_counts(conn):
    """{pooled_fund_id: projects stored}; empty before the first load."""
    from sqlalchemy import text

    if conn.execute(text(f"select to_regclass('{SCHEMA}.cbpf_project')")).scalar() is None:
        return {}
    return dict(conn.execute(text(
        f"select pooled_fund_id, count(*) from {SCHEMA}.cbpf_project group by 1"
    )).all())


def load(conn, projects, clusters, subips, keep_fund_ids):
    """Replace all three tables, except the rows of the pooled funds in
    `keep_fund_ids` (funds whose fetch failed), which stay as they were."""
    from sqlalchemy import text

    def chunked_insert(table, cols, rows, size=1000):
        stmt = text(
            f"insert into {SCHEMA}.{table} ({', '.join(cols)}) "
            f"values ({', '.join(':' + c for c in cols)})"
        )
        for i in range(0, len(rows), size):
            conn.execute(stmt, rows[i:i + size])

    for stmt in [s for s in DDL.split(";\n") if s.strip()]:
        conn.execute(text(stmt))
    if keep_fund_ids:
        # the child tables carry only the project code, so delete them first,
        # while cbpf_project still says which fund each code belongs to
        kept = (f"select chf_project_code from {SCHEMA}.cbpf_project "
                f"where pooled_fund_id = any(:keep)")
        keep = {"keep": list(keep_fund_ids)}
        for t in ("cbpf_project_subip", "cbpf_project_cluster"):
            conn.execute(text(f"delete from {SCHEMA}.{t} "
                              f"where chf_project_code not in ({kept})"), keep)
        conn.execute(text(f"delete from {SCHEMA}.cbpf_project where "
                          f"pooled_fund_id is null or pooled_fund_id <> all(:keep)"),
                     keep)
    else:
        for t in ("cbpf_project_subip", "cbpf_project_cluster", "cbpf_project"):
            conn.execute(text(f"truncate {SCHEMA}.{t}"))
    chunked_insert("cbpf_project", PROJECT_COLUMNS, projects)
    chunked_insert("cbpf_project_cluster", CLUSTER_COLUMNS, clusters)
    chunked_insert("cbpf_project_subip", SUBIP_COLUMNS, subips)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    projects, clusters, subips, failed, empty = fetch_all()
    print(f"normalized: {len(projects)} projects, {len(clusters)} cluster rows, "
          f"{len(subips)} sub-implementing-partner rows")
    if args.dry_run and empty:
        print(f"  note: no projects for {', '.join(empty)} (not checked against "
              f"the stored rows in --dry-run)")
    if not args.dry_run:
        import ocha_stratus as stratus

        eng = stratus.get_engine(stage="dev", write=True)
        with eng.begin() as c:
            # an empty answer for a fund that has stored projects is a failed
            # fetch too; a new envelope with nothing stored is really empty
            stored = stored_counts(c)
            for ab, ids in empty.items():
                n = sum(stored.get(i, 0) for i in ids)
                if n:
                    print(f"  ERROR {ab}: the API returned no projects but {n} are "
                          f"stored — keeping them", file=sys.stderr)
                    failed[ab] = ids
            load(c, projects, clusters, subips, set().union(*failed.values()))
        print(f"loaded aa.cbpf_project ({len(projects)}), aa.cbpf_project_cluster "
              f"({len(clusters)}), aa.cbpf_project_subip ({len(subips)})")
    if failed:
        sys.exit(f"ERROR: {len(failed)} pooled fund(s) failed to fetch: "
                 f"{', '.join(failed)} — their previous rows were kept and every "
                 f"other fund was refreshed. Rerun task refresh_mirrors once the "
                 f"CBPF API answers.")


if __name__ == "__main__":
    main()
