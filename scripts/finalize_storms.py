"""
One-off finalisation:
  * flag definite non-tropical-cyclone storm allocations as not_tc (inland /
    non-basin countries, tornado, winter storm — will never be in IBTrACS)
  * backfill two matches found in allocation summaries that the title-only
    parser missed (Malawi → Freddy; Vanuatu → Judy + Kevin)

Run with --write to commit to the blob.
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

os.environ.setdefault("PGSSLMODE", "require")
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.storage import (  # noqa: E402
    decode_sids,
    encode_sids,
    load_supplemental,
    save_supplemental,
    upsert_annotation,
)

# Definitely not a tropical cyclone (no TC basin / explicit non-TC event)
NOT_TC = [
    "19-RR-CUB-34583",   # Cuba 2019 — tornado
    "20-RR-PAK-41273",   # Pakistan 2020 — winter emergency
    "07-RR-GHA-5535",    # Ghana 2007 — Gulf of Guinea, no TCs
    "07-RR-MLI-7117",    # Mali 2007 — landlocked Sahel
    "07-RR-SDN-13738",   # Sudan 2007 — floods, no TCs
    "07-RR-SDN-10078",   # Sudan 2007 — floods, no TCs
    "07-RR-RWA-10462",   # Rwanda 2007 — landlocked
    "07-RR-UGA-11920",   # Uganda 2007 — landlocked
    "10-RR-BOL-453",     # Bolivia 2010 — landlocked
]

# Matches confirmed by the allocation summary (title-only parser missed these)
SID_FILLS = {
    "23-UF-MWI-61200": ["2023036S12117"],                  # Malawi — TC Freddy
    "23-RR-VUT-58018": ["2023055S14184", "2023059S15149"],  # Vanuatu — Judy + Kevin
}


def main(write: bool):
    supp = load_supplemental()

    def existing(code):
        row = supp[supp["ApplicationCode"] == code]
        return row.iloc[0].to_dict() if not row.empty else {}

    print("=== flag not_tc ===")
    for code in NOT_TC:
        print(f"  {code}")
        keep = existing(code)
        supp = upsert_annotation(supp, code, {
            "sids": keep.get("sids"),  # preserve (should be empty)
            "not_tc": True,
            "valid_month_start": keep.get("valid_month_start"),
            "valid_year_start": keep.get("valid_year_start"),
            "valid_month_end": keep.get("valid_month_end"),
            "valid_year_end": keep.get("valid_year_end"),
            "notes": keep.get("notes"),
        })

    print("=== backfill matches from summaries ===")
    for code, sids in SID_FILLS.items():
        print(f"  {code} -> {sids}")
        keep = existing(code)
        supp = upsert_annotation(supp, code, {
            "sids": encode_sids(sids),
            "not_tc": None,
            "valid_month_start": keep.get("valid_month_start"),
            "valid_year_start": keep.get("valid_year_start"),
            "valid_month_end": keep.get("valid_month_end"),
            "valid_year_end": keep.get("valid_year_end"),
            "notes": keep.get("notes"),
        })

    if not write:
        print("\n(dry run — pass --write to save)")
        return

    save_supplemental(supp)
    n_sid = sum(1 for _, r in supp.iterrows() if decode_sids(r["sids"]))
    n_nottc = sum(1 for _, r in supp.iterrows() if bool(r.get("not_tc")))
    print(f"\nSaved. Blob: {len(supp)} rows — {n_sid} with SID, {n_nottc} not-a-TC.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    main(ap.parse_args().write)
