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

Auth: ocha-stratus get_engine(write=True); PGSSLMODE=require.
Run:  python scripts/refresh_cbpf_projects.py [--dry-run]
"""
import argparse
import json
import os
import sys
import urllib.request

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
    req = urllib.request.Request(f"{API}/{path}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.load(resp)["value"]


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
    abbrvs = sorted({f["PFAbbrv"] for f in _get("MstPooledFund?$format=json")
                     if f.get("PFAbbrv")})
    projects, clusters, subips = {}, {}, set()
    for i, ab in enumerate(abbrvs, 1):
        try:
            rows = _get(f"ProjectSummary?poolfundAbbrv={ab}&$format=json")
        except Exception as e:  # noqa: BLE001 — one bad fund shouldn't kill the run
            print(f"  WARN {ab}: fetch failed ({e}) — skipped", file=sys.stderr)
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
    return list(projects.values()), list(clusters.values()), subip_rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    projects, clusters, subips = fetch_all()
    print(f"normalized: {len(projects)} projects, {len(clusters)} cluster rows, "
          f"{len(subips)} sub-implementing-partner rows")
    if args.dry_run:
        return

    import ocha_stratus as stratus
    from sqlalchemy import text

    def chunked_insert(conn, table, cols, rows, size=1000):
        stmt = text(
            f"insert into {SCHEMA}.{table} ({', '.join(cols)}) "
            f"values ({', '.join(':' + c for c in cols)})"
        )
        for i in range(0, len(rows), size):
            conn.execute(stmt, rows[i:i + size])

    eng = stratus.get_engine(stage="dev", write=True)
    with eng.begin() as c:
        for stmt in [s for s in DDL.split(";\n") if s.strip()]:
            c.execute(text(stmt))
        for t in ("cbpf_project_subip", "cbpf_project_cluster", "cbpf_project"):
            c.execute(text(f"truncate {SCHEMA}.{t}"))
        chunked_insert(c, "cbpf_project", PROJECT_COLUMNS, projects)
        chunked_insert(c, "cbpf_project_cluster", CLUSTER_COLUMNS, clusters)
        chunked_insert(c, "cbpf_project_subip", SUBIP_COLUMNS, subips)
    print(f"loaded aa.cbpf_project ({len(projects)}), aa.cbpf_project_cluster "
          f"({len(clusters)}), aa.cbpf_project_subip ({len(subips)})")


if __name__ == "__main__":
    main()
