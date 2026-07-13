"""Refresh the OneGMS mirror table `aa.cerf_allocation` from the CERF feed.

This is the daily upstream of the storm matcher: it keeps `aa.cerf_allocation` (the
pure OneGMS mirror) current so newly-published allocations become matchable. The
storm tables (`aa.cerf_allocation_storm`, `aa.cerf_supplement`) and every matcher
join to this table on `application_code`.

Coexistence with the KB loader (ds-knowledge-base `scripts/load_aa_cerf.py`):
  - `aa.cerf_allocation`'s schema is canonically OWNED by the KB loader, which also
    builds the AA layer (`aa.actual_activation` / `aa.activation_allocation`) and sets
    the curated `aa_adhoc` / `aa_note` flags. Run it by hand after new activations.
  - This script writes ONLY the feed-derived columns (+ the deterministic `aa_keyword`)
    via an idempotent UPSERT keyed on `application_code`. It deliberately does NOT touch
    `aa_adhoc` / `aa_note` (preserved on conflict) and never touches the AA-link tables,
    so the two writers don't clobber each other — both read the same OneGMS feed, and
    KB curation survives a daily refresh.

Keyed on ApplicationCode; ApplicationID is NOT unique in the feed (~431 collisions).

Auth: ocha-stratus get_engine(write=True); needs DSCI_AZ_DB_DEV_* (+ _WRITE) env and
PGSSLMODE=require. Run:  python scripts/refresh_mirror.py [--xml PATH] [--dry-run]
"""
import argparse
import os
import re
import sys
import xml.etree.ElementTree as ET

os.environ.setdefault("PGSSLMODE", "require")

CERF_API_URL = "https://cerfgms-webapi.unocha.org/v1/application/All.xml"
SCHEMA = "aa"
AA_KEYWORDS = ("anticipat", "early action")

# Feed-derived columns written on every refresh (matches ds-knowledge-base
# load_aa_cerf.py). aa_adhoc / aa_note are KB-curated and deliberately excluded.
FEED_COLUMNS = [
    "application_code", "application_id", "year", "country_iso3", "country_name",
    "region_name", "window_name", "emergency_type", "emergency_group", "title",
    "allocation_status", "agencies", "amount_requested", "amount_approved",
    "individuals_affected", "individuals_planned", "individuals_reached",
    "erc_endorsement_date", "first_project_approved_date", "last_project_approved_date",
    "report_due_date", "aa_keyword", "summary", "humanitarian_overview",
    "allocation_rationale",
]

# Canonical schema lives in the KB loader; kept here (IF NOT EXISTS) so a fresh dev DB
# works. No-op when the table already exists.
DDL = f"""
create schema if not exists {SCHEMA};
create table if not exists {SCHEMA}.cerf_allocation (
    application_code    text primary key,
    application_id      int,
    year                int,
    country_iso3        text,
    country_name        text,
    region_name         text,
    window_name         text,
    emergency_type      text,
    emergency_group     text,
    title               text,
    allocation_status   text,
    agencies            text,
    amount_requested    numeric,
    amount_approved     numeric,
    individuals_affected bigint,
    individuals_planned  bigint,
    individuals_reached  bigint,
    erc_endorsement_date        date,
    first_project_approved_date date,
    last_project_approved_date  date,
    report_due_date             date,
    aa_keyword          boolean not null default false,
    aa_adhoc            boolean not null default false,
    aa_note             text,
    summary             text,
    humanitarian_overview text,
    allocation_rationale  text
);
alter table {SCHEMA}.cerf_allocation add column if not exists aa_adhoc boolean not null default false;
alter table {SCHEMA}.cerf_allocation add column if not exists aa_note text;
"""


def _txt(el, tag):
    c = el.find(tag)
    return c.text.strip() if c is not None and c.text and c.text.strip() else None


def _date(s):
    return s[:10] if s and re.match(r"\d{4}-\d{2}-\d{2}", s) else None


def _int(s):
    try:
        return int(float(s)) if s else None
    except ValueError:
        return None


def _num(s):
    try:
        return float(s) if s else None
    except ValueError:
        return None


def fetch_cerf(xml_path=None):
    """All applications from the OneGMS feed -> mirror rows, keyed on ApplicationCode."""
    if xml_path:
        root = ET.parse(xml_path).getroot()
    else:
        import requests
        resp = requests.get(CERF_API_URL, timeout=300)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    rows = []
    for a in root.findall("application"):
        title, emerg = _txt(a, "ApplicationTitle"), _txt(a, "EmergencyTypeName")
        hay = f"{title or ''} {emerg or ''}".lower()
        rows.append(dict(
            application_code=_txt(a, "ApplicationCode"),
            application_id=_int(_txt(a, "ApplicationID")),
            year=_int(_txt(a, "Year")),
            country_iso3=_txt(a, "CountryCode"),
            country_name=_txt(a, "CountryName"),
            region_name=_txt(a, "RegionName"),
            window_name=_txt(a, "WindowFullName"),
            emergency_type=emerg,
            emergency_group=_txt(a, "EmergencyGroupForGlobalReporting"),
            title=title,
            allocation_status=_txt(a, "AllocationStatus"),
            agencies=_txt(a, "AgencyShortName"),
            amount_requested=_num(_txt(a, "CN_AmountRequested")),
            amount_approved=_num(_txt(a, "TotalAmountApproved")),
            individuals_affected=_int(_txt(a, "TotalIndividualsAffected")),
            individuals_planned=_int(_txt(a, "TotalIndividualPlanned")),
            individuals_reached=_int(_txt(a, "TotalIndividualReached")),
            erc_endorsement_date=_date(_txt(a, "CN_ERC_EndorsementDate")),
            first_project_approved_date=_date(_txt(a, "FirstProjectApprovedDate")),
            last_project_approved_date=_date(_txt(a, "LastProjectApprovedDate")),
            report_due_date=_date(_txt(a, "ReportDueDate")),
            aa_keyword=any(k in hay for k in AA_KEYWORDS),
            summary=_txt(a, "CN_Summary"),
            humanitarian_overview=_txt(a, "OverviewoftheHumanitarianSituation"),
            allocation_rationale=_txt(a, "RationaleforCERFAllocation"),
        ))
    codes = [r["application_code"] for r in rows]
    if None in codes:
        sys.exit("feed has an application with no ApplicationCode — investigate before loading.")
    dupes = len(codes) - len(set(codes))
    if dupes:
        sys.exit(f"ApplicationCode no longer unique in the feed ({dupes} dupes) — investigate before loading.")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xml", help="parse a saved All.xml instead of hitting the API")
    ap.add_argument("--dry-run", action="store_true", help="fetch + report; no DB writes")
    args = ap.parse_args()

    rows = fetch_cerf(args.xml)
    n_aa = sum(r["aa_keyword"] for r in rows)
    print(f"fetched {len(rows)} CERF applications ({n_aa} AA-keyword) from the OneGMS feed")
    if args.dry_run:
        return

    import ocha_stratus as stratus
    from sqlalchemy import text

    set_clause = ", ".join(
        f"{c} = excluded.{c}" for c in FEED_COLUMNS if c != "application_code"
    )
    insert_sql = text(
        f"insert into {SCHEMA}.cerf_allocation ({', '.join(FEED_COLUMNS)}) "
        f"values ({', '.join(':' + c for c in FEED_COLUMNS)}) "
        f"on conflict (application_code) do update set {set_clause}"
    )
    payload = [{c: r[c] for c in FEED_COLUMNS} for r in rows]

    eng = stratus.get_engine(stage="dev", write=True)
    with eng.begin() as c:
        for stmt in [s for s in DDL.split(";\n") if s.strip()]:
            c.execute(text(stmt))
        before = c.execute(text(f"select count(*) from {SCHEMA}.cerf_allocation")).scalar()
        c.execute(insert_sql, payload)
        after = c.execute(text(f"select count(*) from {SCHEMA}.cerf_allocation")).scalar()
    print(f"mirror upserted: {len(rows)} feed rows applied · table {before} -> {after} rows "
          f"(+{after - before} new; aa_adhoc/aa_note preserved)")


if __name__ == "__main__":
    main()
