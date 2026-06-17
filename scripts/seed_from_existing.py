"""
One-off script: pre-populate supplemental data from the existing
cerf-storms-with-sids-2024-02-27.csv (58 SIDs, one per CERF allocation).

Matching strategy:
  1. Primary: CountryName + date + amount (exact)
  2. Fallback: CountryName + amount (dates in the CSV are sometimes off by
     days/weeks from the CERF API endorsement date)

Existing annotations in the blob are NOT overwritten.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import xml.etree.ElementTree as ET

os.environ.setdefault("PGSSLMODE", "require")

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.storage import BLOB_NAME, CONTAINER, STAGE, load_supplemental, save_supplemental

CERF_API_URL = "https://cerfgms-webapi.unocha.org/v1/application/All.xml"
CSV_PATH = Path(
    "/Users/tdowning/OCHA/repos/ds-glb-tropicalcyclones-app/data/"
    "cerf-storms-with-sids-2024-02-27.csv"
)


def fetch_cerf() -> pd.DataFrame:
    print("Fetching CERF API…")
    resp = requests.get(CERF_API_URL, timeout=120)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    rows = []
    for el in root.findall("application"):
        def t(tag):
            ch = el.find(tag)
            return ch.text.strip() if ch is not None and ch.text else None
        rows.append({
            "ApplicationID": t("ApplicationID"),
            "CountryName": t("CountryName"),
            "amount_int": None,
            "alloc_date": None,
            "_amount_raw": t("TotalAmountApproved"),
            "_date_raw": t("CN_ERC_EndorsementDate"),
        })
    df = pd.DataFrame(rows)
    df["amount_int"] = pd.to_numeric(df["_amount_raw"], errors="coerce").round().astype("Int64")
    df["alloc_date"] = pd.to_datetime(df["_date_raw"], errors="coerce").dt.date
    return df.drop(columns=["_amount_raw", "_date_raw"])


def main():
    cerf = fetch_cerf()
    print(f"  {len(cerf)} CERF allocations")

    csv = pd.read_csv(CSV_PATH)
    csv = csv[csv["sid"].notna()].copy()
    csv["alloc_date"] = pd.to_datetime(csv["Allocation date"], errors="coerce").dt.date
    csv["amount_int"] = csv["Amount in US$"].astype("Int64")
    print(f"\nCSV: {len(csv)} rows with SID")

    cerf_indexed_date = cerf.set_index(["CountryName", "alloc_date", "amount_int"])["ApplicationID"]
    cerf_indexed_amt  = cerf.set_index(["CountryName", "amount_int"])["ApplicationID"]

    results = {}  # ApplicationID -> sid
    unmatched = []

    for _, row in csv.iterrows():
        country, date, amt, sid = row["Country"], row["alloc_date"], row["amount_int"], row["sid"]

        # Try primary: country + date + amount
        key_full = (country, date, amt)
        if key_full in cerf_indexed_date.index:
            app_id = cerf_indexed_date[key_full]
            results[str(app_id)] = sid
            continue

        # Fallback: country + amount only
        key_amt = (country, amt)
        if key_amt in cerf_indexed_amt.index:
            app_id = cerf_indexed_amt[key_amt]
            results[str(app_id)] = sid
            print(f"  Fallback match (amt only): {country} {date} ${amt} -> {app_id}")
            continue

        unmatched.append(row)

    print(f"\nMatched: {len(results)}  Unmatched: {len(unmatched)}")
    if unmatched:
        print("  Unmatched rows:")
        for r in unmatched:
            print(f"    {r['Country']}  {r['alloc_date']}  ${r['amount_int']}  {r['sid']}")

    # Load existing, skip already-annotated
    existing = load_supplemental()
    already = set(existing["ApplicationID"]) if not existing.empty else set()
    new_results = {k: v for k, v in results.items() if k not in already}
    print(f"\nAlready in blob: {len(already)}  New to add: {len(new_results)}")

    if not new_results:
        print("Nothing new to write.")
        return

    new_rows = pd.DataFrame([
        {
            "ApplicationID": app_id,
            "sid": sid,
            "valid_month_start": None,
            "valid_year_start": None,
            "valid_month_end": None,
            "valid_year_end": None,
            "notes": None,
            "updated_at": datetime.now(timezone.utc),
        }
        for app_id, sid in new_results.items()
    ])

    updated = pd.concat([existing, new_rows], ignore_index=True)
    save_supplemental(updated)
    print(f"Saved {len(new_rows)} new rows to blob ({CONTAINER}/{BLOB_NAME}, stage={STAGE})")


if __name__ == "__main__":
    main()
