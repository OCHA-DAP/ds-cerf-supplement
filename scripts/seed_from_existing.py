"""
One-off script: (re)build supplemental SID data from the existing
cerf-storms-with-sids-2024-02-27.csv (58 storm allocations with SIDs).

Matching strategy (CSV row -> CERF ApplicationID):
  Key on (CountryName, exact USD amount). Amount is unique across CERF except
  for two round numbers; those are disambiguated by nearest endorsement date.

Every resulting (ApplicationID -> sid) pair is verified against IBTrACS
(storm season must be within 1 year of the allocation year). Pairs that fail
verification are reported and excluded.

This OVERWRITES the supplemental blob (SID column). Run with --write to commit.
"""

import argparse
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

os.environ.setdefault("PGSSLMODE", "require")

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.db import load_storms  # noqa: E402
from src.storage import (  # noqa: E402
    BLOB_NAME,
    CONTAINER,
    STAGE,
    load_supplemental,
    save_supplemental,
)

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
            "ApplicationCode": t("ApplicationCode"),
            "CountryName": t("CountryName"),
            "Year": pd.to_numeric(t("Year"), errors="coerce"),
            "amount_int": pd.to_numeric(t("TotalAmountApproved"), errors="coerce"),
            "alloc_date": pd.to_datetime(t("CN_ERC_EndorsementDate"), errors="coerce"),
        })
    df = pd.DataFrame(rows)
    df["amount_int"] = df["amount_int"].round().astype("Int64")
    return df


def main(write: bool):
    cerf = fetch_cerf()
    print(f"  {len(cerf)} CERF allocations")

    csv = pd.read_csv(CSV_PATH)
    csv = csv[csv["sid"].notna()].copy()
    csv["amount_int"] = csv["Amount in US$"].astype("Int64")
    csv["alloc_date"] = pd.to_datetime(csv["Allocation date"], errors="coerce")
    print(f"\nCSV: {len(csv)} rows with SID")

    pairs = []  # (ApplicationID, sid, ApplicationCode, country, year)
    unmatched = []
    for _, r in csv.iterrows():
        cands = cerf[
            (cerf["CountryName"] == r["Country"])
            & (cerf["amount_int"] == r["amount_int"])
        ]
        if len(cands) == 0:
            unmatched.append((r["Country"], r["amount_int"], r["sid"]))
            continue
        if len(cands) > 1:
            # disambiguate by nearest endorsement date
            cands = cands.assign(
                _gap=(cands["alloc_date"] - r["alloc_date"]).abs()
            ).sort_values("_gap")
        c = cands.iloc[0]
        pairs.append((c["ApplicationID"], r["sid"], c["ApplicationCode"], c["CountryName"], c["Year"]))

    matched = pd.DataFrame(pairs, columns=["ApplicationID", "sid", "ApplicationCode", "CountryName", "Year"])
    matched = matched.drop_duplicates(subset=["ApplicationID"], keep="first")
    print(f"Matched: {len(matched)}  Unmatched: {len(unmatched)}")
    for c, a, s in unmatched:
        print(f"  UNMATCHED: {c} ${a} {s}")

    # --- Verify every pair ---
    # Base the check on the year encoded in the SID prefix (YYYY...), which is
    # independent of the DB and works even for unnamed storms. Merge in the
    # IBTrACS name/season for display only.
    storms = load_storms.__wrapped__()  # sid, name, season
    v = matched.merge(storms, on="sid", how="left")
    v["Year"] = v["Year"].astype("Int64")
    v["season"] = v["season"].astype("Int64")
    v["sid_year"] = v["sid"].str[:4].astype(int)
    v["year_gap"] = (v["sid_year"] - v["Year"]).abs()
    v["ok"] = v["year_gap"] <= 1

    bad = v[~v["ok"].fillna(False)]
    print(f"\nVerification: {v['ok'].sum()} OK, {len(bad)} FAILED")
    if not bad.empty:
        print(bad[["ApplicationCode", "CountryName", "Year", "name", "season", "sid"]].to_string(index=False))

    good = v[v["ok"]].copy()
    print(f"\n=== {len(good)} verified pairs ===")
    print(good[["ApplicationCode", "CountryName", "Year", "name", "season", "sid"]].sort_values("ApplicationCode").to_string(index=False))

    if not write:
        print("\n(dry run — pass --write to save)")
        return

    new_rows = pd.DataFrame({
        "ApplicationID": good["ApplicationID"].values,
        "sid": good["sid"].values,
        "valid_month_start": None,
        "valid_year_start": None,
        "valid_month_end": None,
        "valid_year_end": None,
        "notes": None,
        "updated_at": datetime.now(timezone.utc),
    })

    # Preserve only genuine drought annotations (a month or year set); drop any
    # prior SID-only rows so a re-seed fully replaces the (possibly stale) SIDs.
    existing = load_supplemental()
    drought_cols = ["valid_month_start", "valid_year_start", "valid_month_end", "valid_year_end"]
    if not existing.empty:
        has_drought = existing[drought_cols].notna().any(axis=1)
        keep = existing[has_drought & ~existing["ApplicationID"].isin(new_rows["ApplicationID"])]
        out = pd.concat([keep, new_rows], ignore_index=True)
    else:
        out = new_rows
    save_supplemental(out)
    print(f"\nSaved {len(new_rows)} SID rows ({len(out)} total) to {CONTAINER}/{BLOB_NAME} (stage={STAGE})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    main(ap.parse_args().write)
