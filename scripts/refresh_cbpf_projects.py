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

Any pooled fund id that has stored rows but no project in this run's data keeps its
previous rows, and every other fund is replaced as usual. That covers a failed
fetch, an empty answer, a fund missing from `MstPooledFund`, and AP501 answering for
only some of its country envelopes. The script then exits non-zero naming the
fund(s), so the job's failure email fires (before 2026-10, such a fund was silently
dropped from all three tables on a green run). Not caught: a partial answer under
the same fund id. If a fund really has left the API, drop its rows once with
`--forget-fund`.

Auth: ocha-stratus get_engine(write=True); PGSSLMODE=require.
Run:  python scripts/refresh_cbpf_projects.py [--dry-run | --forget-fund ID ...]
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
    """Returns (projects, clusters, subips, failed); `failed` lists the
    abbreviations of the funds whose fetch failed."""
    # one abbreviation can cover several fund ids: the Asia-Pacific regional fund
    # (AP501) is listed once per country envelope, and its projects carry those
    # ids (94, 95, 97, 99, 505), not 501
    fund_ids = {}
    for f in _get("MstPooledFund?$format=json"):
        if f.get("PFAbbrv"):
            fund_ids.setdefault(f["PFAbbrv"], set()).add(f["PFId"])
    if not fund_ids:
        sys.exit("ERROR: MstPooledFund listed no pooled funds — nothing fetched or "
                 "loaded.")
    abbrvs = sorted(fund_ids)
    projects, clusters, subips = {}, {}, set()
    failed = []
    for i, ab in enumerate(abbrvs, 1):
        try:
            rows = _get(f"ProjectSummary?poolfundAbbrv={ab}&$format=json")
        # _fetch_feed has already retried; a truncated body gets past
        # raise_for_status and fails here as bad JSON (ValueError)
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"  ERROR {ab}: fetch failed ({e}) — its stored rows are kept",
                  file=sys.stderr)
            failed.append(ab)
            continue
        print(f"  [{i}/{len(abbrvs)}] {ab}: {len(rows)} expanded rows")
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
    return list(projects.values()), list(clusters.values()), subip_rows, failed


def missing_funds(stored, projects):
    """The stored pooled fund ids with no project in this run's data, as
    {id: (name, projects stored)} from `stored` (same shape)."""
    fetched = {p["pooled_fund_id"] for p in projects}
    return {i: v for i, v in stored.items() if i not in fetched}


def load(conn, projects, clusters, subips):
    """Replace all three tables, except the rows of every stored pooled fund id
    with no project in this run's data, which stay as they were. Returns those
    ids as {id: (name, projects kept)}."""
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
    stored = {i: (name, n) for i, name, n in conn.execute(text(
        f"select pooled_fund_id, max(pooled_fund_name), count(*) "
        f"from {SCHEMA}.cbpf_project where pooled_fund_id is not null group by 1"
    )).all()}
    keep_fund_ids = missing_funds(stored, projects)
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
    return keep_fund_ids


def forget(conn, fund_ids):
    """Delete these pooled fund ids' rows from all three tables (children first);
    returns {table: rows deleted}."""
    from sqlalchemy import text

    ids = {"ids": list(fund_ids)}
    codes = (f"select chf_project_code from {SCHEMA}.cbpf_project "
             f"where pooled_fund_id = any(:ids)")
    deleted = {}
    for t in ("cbpf_project_subip", "cbpf_project_cluster"):
        deleted[t] = conn.execute(text(f"delete from {SCHEMA}.{t} "
                                       f"where chf_project_code in ({codes})"),
                                  ids).rowcount
    deleted["cbpf_project"] = conn.execute(text(
        f"delete from {SCHEMA}.cbpf_project where pooled_fund_id = any(:ids)"), ids
    ).rowcount
    return deleted


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--forget-fund", type=int, nargs="+", metavar="ID",
                      help="delete these pooled fund ids' rows from all three tables "
                      "and exit, without fetching (for a fund that really left the "
                      "API, which otherwise keeps the job red)")
    args = ap.parse_args()

    if args.forget_fund:
        import ocha_stratus as stratus

        with stratus.get_engine(stage="dev", write=True).begin() as c:
            deleted = forget(c, args.forget_fund)
        print(f"forgot pooled fund id(s) {', '.join(map(str, args.forget_fund))}: "
              + ", ".join(f"aa.{t} {n}" for t, n in deleted.items()))
        return

    projects, clusters, subips, failed = fetch_all()
    print(f"normalized: {len(projects)} projects, {len(clusters)} cluster rows, "
          f"{len(subips)} sub-implementing-partner rows")
    kept = {}
    if not args.dry_run:
        import ocha_stratus as stratus

        eng = stratus.get_engine(stage="dev", write=True)
        with eng.begin() as c:
            kept = load(c, projects, clusters, subips)
        print(f"loaded aa.cbpf_project ({len(projects)}), aa.cbpf_project_cluster "
              f"({len(clusters)}), aa.cbpf_project_subip ({len(subips)})")
    problems = []
    if kept:
        problems.append(
            "kept the previous rows of pooled fund id(s) with no project in this "
            "run: " + "; ".join(f"{name} (id {i}, {n} projects)"
                                for i, (name, n) in sorted(kept.items())))
    if failed:
        problems.append(f"fetch failed for {', '.join(failed)}")
    if problems:
        sys.exit("ERROR: CBPF project mirror incomplete — " + "; ".join(problems)
                 + ". Every other fund was refreshed. Rerun task refresh_mirrors once "
                 "the CBPF API answers; if a fund really left the API, drop its rows "
                 "once with `scripts/refresh_cbpf_projects.py --forget-fund <id> ...`.")


if __name__ == "__main__":
    main()
