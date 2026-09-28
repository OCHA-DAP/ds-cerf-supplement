"""Refresh the donor-contribution mirrors: who paid into CERF and into each CBPF.

Donor attribution of anticipatory-action money ("donor shares": a donor's share of a
fund's income in a fiscal year, times the AA the fund released / pre-arranged that year)
needs contributions per fund × donor × year. Both feeds are public OneGMS surfaces:

- CERF: https://cerfgms-webapi.unocha.org/v1/donorcontribution.json — one row per
  contribution (pledge / commitment / received / write-off legs — each a nested list of
  USD amounts, summed here — plus status and multi-year agreement flags), keyed
  `contributionCode`. Republished weekly on HDX as
  "Global - CERF Donor Contributions". Rows are upserted (codes are stable).
- CBPF / regional funds: https://cbpfapi.unocha.org/vo2/odata/ContributionTotal — donor
  totals per pooled fund × fiscal year (paid + pledged). The feed has a handful of
  duplicate (year, fund, donor) rows (e.g. DRC / Sweden 2022 twice) — they are SUMMED at
  load, so the table is one row per (fiscal_year, pooled_fund_name, donor). The feed
  carries no pooled-fund id; it is resolved by name against MstPooledFund (the same
  registry `aa.cbpf_fund` mirrors). Full-replace load.

Writes two pure mirror tables (this script is their SOLE writer):
- `aa.cerf_contribution`
- `aa.cbpf_contribution`

and (re)creates `aa.v_contribution` — the fund-agnostic UNION view, normalized to
(fund_type, fund_name, pooled_fund_id, donor, year, paid_usd, pledged_usd) with donor
names harmonized across the two feeds (`donor_raw` keeps each feed's spelling).
"Paid" is cash the fund received in that fiscal year (CERF `donorreceived`, CBPF
`PaidAmt`) — the base the donor-share method uses; pledges are carried alongside.

Auth: ocha-stratus get_engine(write=True); needs DSCI_AZ_DB_DEV_* (+ _WRITE) env and
PGSSLMODE=require. Run:  python scripts/refresh_contributions.py [--dry-run]
"""
import argparse
import json
import os
import sys
import urllib.request
from collections import defaultdict

os.environ.setdefault("PGSSLMODE", "require")

CERF_URL = "https://cerfgms-webapi.unocha.org/v1/donorcontribution.json"
CBPF_API = "https://cbpfapi.unocha.org/vo2/odata"
SCHEMA = "aa"

CERF_COLUMNS = [
    "contribution_code", "contribution_id", "donor", "donor_iso3", "region_name",
    "donor_type", "year", "status_code", "contribution_status", "activity_date",
    "activity_date_type", "latest_date", "pledge_usd", "commitment_usd",
    "received_usd", "n_receipts", "last_received_date", "writeoff_usd",
    "commitment_type", "agreement_from_year", "agreement_to_year",
]
CBPF_COLUMNS = [
    "fiscal_year", "pooled_fund_name", "donor", "pooled_fund_id", "pooled_fund_iso2",
    "donor_iso2", "paid_usd", "pledged_usd", "n_feed_rows", "has_transfer",
]

DDL = f"""
create schema if not exists {SCHEMA};
create table if not exists {SCHEMA}.cerf_contribution (
    contribution_code   text primary key,          -- e.g. 25-DC-SWE-001
    contribution_id     int,
    donor               text,
    donor_iso3          text,      -- countryCode; pseudo-codes PRV (private), UNF (via UN Foundation)
    region_name         text,
    donor_type          text,      -- Member State | Observer | Regional Local Authority | Private | ...
    year                int,       -- CERF fiscal year the contribution counts toward
    status_code         int,
    contribution_status text,      -- Fully Paid | Partially Paid | Pledge | Pipeline | Write-Off
    activity_date       date,
    activity_date_type  int,
    latest_date         date,
    pledge_usd          numeric,
    commitment_usd      numeric,
    received_usd        numeric,   -- cash received, summed over receipts (the donor-share base)
    n_receipts          int,
    last_received_date  date,
    writeoff_usd        numeric,
    commitment_type     text,      -- Multi-Year Agreement | Pledge Letter | Agreement | EFT ...
    agreement_from_year int,
    agreement_to_year   int,
    updated_at          timestamptz not null default now()
);
create table if not exists {SCHEMA}.cbpf_contribution (
    fiscal_year       int not null,
    pooled_fund_name  text not null,
    donor             text not null,     -- GMSDonorName
    pooled_fund_id    int,               -- resolved by name via MstPooledFund (null if unmatched)
    pooled_fund_iso2  text,
    donor_iso2        text,              -- GMSDonorISO2Code; pseudo-codes UNF, UN, EU, GB-SC ...
    paid_usd          numeric,           -- cash received in the fiscal year (donor-share base)
    pledged_usd       numeric,
    n_feed_rows       int not null,      -- >1 where the feed repeated the key (summed here)
    has_transfer      boolean not null default false,  -- any feed row flagged IsTransfer
    updated_at        timestamptz not null default now(),
    primary key (fiscal_year, pooled_fund_name, donor)
);
"""

# fund-agnostic union; donor names harmonized so one donor is one row across funds.
# fund_type mirrors v_allocation's rule (regional envelopes by name).
V_CONTRIBUTION = f"""
create or replace view {SCHEMA}.v_contribution as
with u as (
    select 'cerf'::text        as fund_type,
           'CERF'::text        as fund_name,
           null::int           as pooled_fund_id,
           donor               as donor_raw,
           donor_iso3          as donor_iso3,
           null::text          as donor_iso2,
           donor_type,
           year,
           received_usd        as paid_usd,
           pledge_usd          as pledged_usd,
           'cerf_contribution'::text as source_table
    from {SCHEMA}.cerf_contribution
    union all
    select case when pooled_fund_name ilike '%%rhpf%%'
                  or pooled_fund_name ilike '%%regional%%'
                then 'regional_fund' else 'cbpf' end,
           pooled_fund_name,
           pooled_fund_id,
           donor,
           null::text,
           donor_iso2,
           null::text,
           fiscal_year,
           paid_usd,
           pledged_usd,
           'cbpf_contribution'
    from {SCHEMA}.cbpf_contribution
)
select fund_type, fund_name, pooled_fund_id,
       case donor_raw
           when 'United States'                    then 'United States of America'
           when 'Korea'                            then 'Republic of Korea'
           when 'Turkey'                           then 'Türkiye'
           when 'Czech Republic'                   then 'Czechia'
           when 'Private donations (through UNF)'  then 'Private contributions (through UNF)'
           when 'Private Contributions'            then 'Private contributions (through UNF)'
           when 'PRIVATE SECTOR'                   then 'Private contributions (through UNF)'
           when 'Private donations through UN Foundation (under $10,000)'
                                                   then 'Private contributions (through UNF)'
           when 'Funding under $10,000'            then 'Private contributions (through UNF)'
           else donor_raw
       end as donor,
       donor_raw, donor_iso3, donor_iso2, donor_type, year, paid_usd, pledged_usd,
       source_table
from u
"""


def _get_json(url):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.load(resp)


def _date(s):
    return s[:10] if s and str(s)[:4].isdigit() else None


def _int(v):
    return None if v is None or v == "" else int(float(v))


def _leg(r, name, amount_key):
    """The feed nests each money leg as {name: {name: [ {…amountUSD, …date}, … ]}} —
    one contribution can have several receipts. Returns (sum USD, n legs, last date)."""
    legs = ((r.get(name) or {}).get(name)) or []
    usd = sum(x.get(amount_key) or 0.0 for x in legs)
    dates = sorted(_date(x.get(k)) for x in legs for k in x if k.endswith("date")
                   and _date(x.get(k)))
    return usd, len(legs), (dates[-1] if dates else None)


def fetch_cerf():
    rows = []
    for r in _get_json(CERF_URL):
        pledge, _, _ = _leg(r, "donorpledge", "pledgeAmountUSD")
        commit, _, _ = _leg(r, "donorcommitment", "commitmentamountUSD")
        received, n_rec, last_rec = _leg(r, "donorreceived", "receivedamountUSD")
        writeoff, _, _ = _leg(r, "donorwriteoff", "writeoffamountUSD")
        rows.append(dict(
            contribution_code=r["contributionCode"],
            contribution_id=r.get("contributionId"),
            donor=r.get("donor"),
            donor_iso3=r.get("countryCode"),
            region_name=r.get("regionName"),
            donor_type=r.get("donortype"),
            year=_int(r.get("year")),
            status_code=r.get("statusCode"),
            contribution_status=r.get("contributionStatus"),
            activity_date=_date(r.get("activityDate")),
            activity_date_type=r.get("activityDateType"),
            latest_date=_date(r.get("latestDate")),
            pledge_usd=pledge,
            commitment_usd=commit,
            received_usd=received,
            n_receipts=n_rec,
            last_received_date=last_rec,
            writeoff_usd=writeoff,
            commitment_type=r.get("CommitmentType"),
            agreement_from_year=_int(r.get("AgreementFromYear")),
            agreement_to_year=_int(r.get("AgreementToYear")),
        ))
    codes = [r["contribution_code"] for r in rows]
    if len(codes) != len(set(codes)):
        sys.exit("contributionCode no longer unique in the CERF feed — investigate before loading.")
    return rows


def fetch_cbpf():
    funds = {f.get("PFName"): f["PFId"]
             for f in _get_json(f"{CBPF_API}/MstPooledFund?$format=json")["value"]}
    agg = {}
    for r in _get_json(f"{CBPF_API}/ContributionTotal?$format=json")["value"]:
        key = (r["FiscalYear"], r["PooledFundName"], r["GMSDonorName"])
        a = agg.setdefault(key, dict(
            fiscal_year=key[0], pooled_fund_name=key[1], donor=key[2],
            pooled_fund_id=funds.get(key[1]), pooled_fund_iso2=r.get("PooledFundISO2Code"),
            donor_iso2=r.get("GMSDonorISO2Code"), paid_usd=0.0, pledged_usd=0.0,
            n_feed_rows=0, has_transfer=False))
        a["paid_usd"] += r.get("PaidAmt") or 0.0
        a["pledged_usd"] += r.get("PledgeAmt") or 0.0
        a["n_feed_rows"] += 1
        a["has_transfer"] = a["has_transfer"] or bool(r.get("IsTransfer"))
    rows = list(agg.values())
    unmatched = sorted({r["pooled_fund_name"] for r in rows if r["pooled_fund_id"] is None})
    if unmatched:
        print(f"  warning: {len(unmatched)} pooled-fund names not in MstPooledFund "
              f"(pooled_fund_id left null): {unmatched}")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="fetch + report; no DB writes")
    args = ap.parse_args()

    cerf = fetch_cerf()
    cbpf = fetch_cbpf()
    by_year = defaultdict(float)
    for r in cerf:
        by_year[r["year"]] += r["received_usd"] or 0
    dup = sum(r["n_feed_rows"] > 1 for r in cbpf)
    print(f"fetched {len(cerf)} CERF contributions ({min(by_year)}–{max(by_year)}; "
          f"received {by_year.get(2025, 0)/1e6:,.1f}M in 2025) and "
          f"{len(cbpf)} CBPF fund × donor × year totals ({dup} summed duplicates)")
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
            f"on conflict ({key}) do update set {setc}, updated_at = now()"
        )

    eng = stratus.get_engine(stage="dev", write=True)
    with eng.begin() as c:
        for stmt in [s for s in DDL.split(";\n") if s.strip()]:
            c.execute(text(stmt))
        before = c.execute(text(f"select count(*) from {SCHEMA}.cerf_contribution")).scalar()
        c.execute(upsert_sql("cerf_contribution", CERF_COLUMNS, "contribution_code"), cerf)
        after = c.execute(text(f"select count(*) from {SCHEMA}.cerf_contribution")).scalar()
        # CBPF: full replace (aggregate rows have no stable upstream id)
        c.execute(text(f"delete from {SCHEMA}.cbpf_contribution"))
        c.execute(text(
            f"insert into {SCHEMA}.cbpf_contribution ({', '.join(CBPF_COLUMNS)}) "
            f"values ({', '.join(':' + col for col in CBPF_COLUMNS)})"), cbpf)
        c.execute(text(V_CONTRIBUTION))
    print(f"contribution mirrors refreshed: aa.cerf_contribution {before} -> {after} rows; "
          f"aa.cbpf_contribution {len(cbpf)} rows (replaced); view aa.v_contribution refreshed")


if __name__ == "__main__":
    main()
