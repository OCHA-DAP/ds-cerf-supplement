"""Refresh the OneGMS project mirror tables from the CERF project feed.

Companion to refresh_mirror.py (allocations): mirrors the ~8.6k individual agency
projects behind every CERF application from `v1/project/All.json` into three tables
in schema `aa`, joining to `aa.cerf_allocation` on `application_code`:

- `aa.cerf_project`         — one row per project (agency, amounts, dates, status,
                              planned/reached people incl. demographic breakdowns,
                              HRP cap-codes, project summary), UPSERTED keyed on
                              `project_code`.
- `aa.cerf_project_sector`  — per-sector amount split (1-5 rows per project). No PK:
                              the feed has real duplicate (project, sector) rows with
                              split amounts (e.g. 15-UF-FAO-004). Full-replaced.
- `aa.cerf_project_country` — per-country budget split for regional projects (e.g.
                              Venezuela-crisis projects spanning 5+ countries).
                              Full-replaced, PK (project_code, country_iso3).

Like the allocation feed, the numeric ID is NOT unique: `projectID` has ~3.1k
collisions across the feed's two source tables (`tableName` M/P). `projectCode`
(e.g. `06-FAO-010-A`, newer style `CERF-TCD-25-UF-HCR-35482`) is unique — key on it.
`plannedChildren`/`plannedAdults` (and reached equivalents) are not mirrored: they
equal girls+boys / women+men in all but ~10 rows.

This script is the SOLE writer of all three tables; everything runs in one
transaction. The feed takes ~8 min to generate server-side — be patient.

Auth: ocha-stratus get_engine(write=True); needs DSCI_AZ_DB_DEV_* (+ _WRITE) env and
PGSSLMODE=require. Run:  python scripts/refresh_projects.py [--json PATH] [--dry-run]
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("PGSSLMODE", "require")

SCHEMA = "aa"

PROJECT_COLUMNS = [
    "project_code", "project_id", "application_code", "year", "country_iso3",
    "country_name", "region_name", "subregion_name", "window_name",
    "agency_short_name", "agency_name", "implementing_agency_name",
    "emergency_type", "emergency_group", "title", "summary", "project_status",
    "allocation_status", "project_type", "sector_name", "amount_approved",
    "beneficiary_type", "gender_marker_id", "gender_marker_type",
    "gender_marker_name", "start_date", "end_date", "letter_sent_date",
    "usg_signature_date", "disbursement_date",
    "people_planned", "planned_female", "planned_male", "planned_women",
    "planned_men", "planned_girls", "planned_boys",
    "people_reached", "reached_female", "reached_male", "reached_women",
    "reached_men", "reached_girls", "reached_boys",
    "cap_codes", "grouping_names", "source_table",
]

SECTOR_COLUMNS = [
    "project_code", "sector_id", "sector_name", "cerf_sector_name",
    "cluster_name", "iasc_sector_name", "sector_amount",
]

COUNTRY_COLUMNS = ["project_code", "country_iso3", "country_percent", "total_budget"]

# Canonical schema (this script owns all three tables). IF NOT EXISTS — no-op when
# they already exist.
DDL = f"""
create schema if not exists {SCHEMA};
create table if not exists {SCHEMA}.cerf_project (
    project_code        text primary key,
    project_id          int,
    application_code    text,
    year                int,
    country_iso3        text,
    country_name        text,
    region_name         text,
    subregion_name      text,
    window_name         text,
    agency_short_name   text,
    agency_name         text,
    implementing_agency_name text,
    emergency_type      text,
    emergency_group     text,
    title               text,
    summary             text,
    project_status      text,
    allocation_status   text,
    project_type        text,
    sector_name         text,
    amount_approved     numeric,
    beneficiary_type    text,
    gender_marker_id    int,
    gender_marker_type  text,
    gender_marker_name  text,
    start_date          date,
    end_date            date,
    letter_sent_date    date,
    usg_signature_date  date,
    disbursement_date   date,
    people_planned      bigint,
    planned_female      bigint,
    planned_male        bigint,
    planned_women       bigint,
    planned_men         bigint,
    planned_girls       bigint,
    planned_boys        bigint,
    people_reached      bigint,
    reached_female      bigint,
    reached_male        bigint,
    reached_women       bigint,
    reached_men         bigint,
    reached_girls       bigint,
    reached_boys        bigint,
    cap_codes           text,
    grouping_names      text,
    source_table        text
);
create index if not exists cerf_project_application_code_idx
    on {SCHEMA}.cerf_project (application_code);
create table if not exists {SCHEMA}.cerf_project_sector (
    project_code        text not null,
    sector_id           int,
    sector_name         text,
    cerf_sector_name    text,
    cluster_name        text,
    iasc_sector_name    text,
    sector_amount       numeric
);
create index if not exists cerf_project_sector_project_code_idx
    on {SCHEMA}.cerf_project_sector (project_code);
create table if not exists {SCHEMA}.cerf_project_country (
    project_code        text not null,
    country_iso3        text not null,
    country_percent     numeric,
    total_budget        numeric,
    primary key (project_code, country_iso3)
);
"""


def _s(v):
    """Feed string -> stripped text or None (the feed uses '' for missing)."""
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _i(v):
    try:
        return int(v) if v is not None and v != "" else None
    except (ValueError, TypeError):
        return None


def _num(v):
    try:
        return float(v) if v is not None and v != "" else None
    except (ValueError, TypeError):
        return None


def _date(v):
    return v[:10] if v and re.match(r"\d{4}-\d{2}-\d{2}", str(v)) else None


def _joined(items, key):
    """Dedup + join child values the feed nests (cap codes, grouping names)."""
    out, seen = [], set()
    for it in items:
        v = _s(it.get(key))
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return ";".join(out) or None


def fetch_projects(json_path=None):
    """All projects from the OneGMS feed -> (project, sector, country) row lists."""
    if json_path:
        feed = json.loads(Path(json_path).read_bytes())
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from src.cerf_api import fetch_project_feed
        feed = json.loads(fetch_project_feed())

    projects, sectors, countries = [], [], []
    for p in feed:
        code = _s(p.get("projectCode"))
        projects.append(dict(
            project_code=code,
            project_id=_i(p.get("projectID")),
            application_code=_s(p.get("applicationCode")),
            year=_i(p.get("year")),
            country_iso3=_s(p.get("countryCode")),
            country_name=_s(p.get("countryName")),
            region_name=_s(p.get("regionName")),
            subregion_name=_s(p.get("subRegionName")),
            window_name=_s(p.get("windowFullName")),
            agency_short_name=_s(p.get("agencyShortName")),
            agency_name=_s(p.get("agencyName")),
            implementing_agency_name=_s(p.get("implementingAgencyName")),
            emergency_type=_s(p.get("emergencyTypeName")),
            emergency_group=_s(p.get("emergencyGroupForGlobalReporting")),
            title=_s(p.get("projectTitle")),
            summary=_s(p.get("projectSummary")),
            project_status=_s(p.get("projectStatus")),
            allocation_status=_s(p.get("allocationStatus")),
            project_type=_s(p.get("projectTypeName")),
            sector_name=_s(p.get("projectSectorName")),
            amount_approved=_num(p.get("totalAmountApproved")),
            beneficiary_type=_s(p.get("beneficiaryType")),
            gender_marker_id=_i(p.get("cerfGenderMarkerID")),
            gender_marker_type=_s(p.get("genderMarkerType")),
            gender_marker_name=_s(p.get("cerfGenderMarkerName")),
            start_date=_date(p.get("projectStartDate")),
            end_date=_date(p.get("projectEndDate")),
            letter_sent_date=_date(p.get("letterSentToAgencyDate")),
            usg_signature_date=_date(p.get("dateUSGSignature")),
            disbursement_date=_date(p.get("disbursementDate")),
            people_planned=_i(p.get("totalPeoplePlanned")),
            planned_female=_i(p.get("plannedFemale")),
            planned_male=_i(p.get("plannedMale")),
            planned_women=_i(p.get("plannedWomen")),
            planned_men=_i(p.get("plannedMen")),
            planned_girls=_i(p.get("plannedGirls")),
            planned_boys=_i(p.get("plannedBoys")),
            people_reached=_i(p.get("totalPeopleReached")),
            reached_female=_i(p.get("reachedFemale")),
            reached_male=_i(p.get("reachedMale")),
            reached_women=_i(p.get("reachedWomen")),
            reached_men=_i(p.get("reachedMen")),
            reached_girls=_i(p.get("reachedGirls")),
            reached_boys=_i(p.get("reachedBoys")),
            cap_codes=_joined(p["projectcapcode"]["projectcapcode"], "capCode"),
            grouping_names=_joined(p["projectgrouping"]["projectgrouping"],
                                   "groupingName"),
            source_table=_s(p.get("tableName")),
        ))
        for s in p["projectsectors"]["projectsectors"]:
            sectors.append(dict(
                project_code=code,
                sector_id=_i(s.get("sectorID")),
                sector_name=_s(s.get("sectorName")),
                cerf_sector_name=_s(s.get("cERFSectorName")),
                cluster_name=_s(s.get("clusterName")),
                iasc_sector_name=_s(s.get("iascSectorname")),
                sector_amount=_num(s.get("sectorAmount")),
            ))
        seen_iso3 = set()
        for c in p["ProjectCountry"]["ProjectCountry"]:
            iso3 = _s(c.get("countryCode"))
            if iso3 in seen_iso3:
                continue
            seen_iso3.add(iso3)
            countries.append(dict(
                project_code=code,
                country_iso3=iso3,
                country_percent=_num(c.get("countryPercent")),
                total_budget=_num(c.get("totalBudget")),
            ))

    codes = [r["project_code"] for r in projects]
    if None in codes:
        sys.exit("feed has a project with no projectCode — investigate before loading.")
    dupes = len(codes) - len(set(codes))
    if dupes:
        sys.exit(f"projectCode no longer unique in the feed ({dupes} dupes) — "
                 "investigate before loading.")
    return projects, sectors, countries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="parse a saved All.json instead of hitting the API")
    ap.add_argument("--dry-run", action="store_true", help="fetch + report; no DB writes")
    args = ap.parse_args()

    projects, sectors, countries = fetch_projects(args.json)
    n_apps = len({r["application_code"] for r in projects})
    print(f"fetched {len(projects)} CERF projects across {n_apps} applications "
          f"({len(sectors)} sector rows, {len(countries)} country rows)")
    if args.dry_run:
        return

    import ocha_stratus as stratus
    from sqlalchemy import text

    def insert_chunked(conn, table, columns, rows, on_conflict="", chunk=500):
        """Multi-row VALUES inserts — plain executemany is one round-trip per row,
        which at ~27k total rows against the Azure DB is unusably slow."""
        for lo in range(0, len(rows), chunk):
            batch = rows[lo:lo + chunk]
            values = ", ".join(
                f"({', '.join(f':{c}_{i}' for c in columns)})"
                for i in range(len(batch))
            )
            params = {f"{c}_{i}": r[c] for i, r in enumerate(batch) for c in columns}
            conn.execute(
                text(f"insert into {SCHEMA}.{table} ({', '.join(columns)}) "
                     f"values {values} {on_conflict}"),
                params,
            )

    set_clause = ", ".join(
        f"{c} = excluded.{c}" for c in PROJECT_COLUMNS if c != "project_code"
    )

    eng = stratus.get_engine(stage="dev", write=True)
    with eng.begin() as c:
        for stmt in [s for s in DDL.split(";\n") if s.strip()]:
            c.execute(text(stmt))
        before = c.execute(text(f"select count(*) from {SCHEMA}.cerf_project")).scalar()
        insert_chunked(c, "cerf_project", PROJECT_COLUMNS, projects,
                       f"on conflict (project_code) do update set {set_clause}")
        c.execute(text(f"delete from {SCHEMA}.cerf_project_sector"))
        insert_chunked(c, "cerf_project_sector", SECTOR_COLUMNS, sectors)
        c.execute(text(f"delete from {SCHEMA}.cerf_project_country"))
        insert_chunked(c, "cerf_project_country", COUNTRY_COLUMNS, countries)
        after = c.execute(text(f"select count(*) from {SCHEMA}.cerf_project")).scalar()
    print(f"project mirror upserted: {len(projects)} feed rows applied · "
          f"aa.cerf_project {before} -> {after} rows (+{after - before} new) · "
          f"sectors {len(sectors)} · countries {len(countries)} (full-replaced)")


if __name__ == "__main__":
    main()
