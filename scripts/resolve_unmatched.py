"""
One-off: resolve the remaining unmatched storm allocations after manual +
web research (2026-07). Confident storm matches and clear non-TCs only;
genuinely uncertain / not-yet-archived allocations are left alone.

Run with --write to commit.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("PGSSLMODE", "require")
from src.storage import (  # noqa: E402
    encode_sids, load_supplemental, save_supplemental, upsert_annotation,
)

# code -> list of IBTrACS SIDs (storm named in the allocation summary, confirmed
# in IBTrACS with matching country + timing)
SID_FILLS = {
    "CERF-CUB-25-RR-1478": ["2025291N11319"],   # Cuba — Hurricane Melissa (Oct 2025)
    "CERF-HTI-24-RR-1399": ["2025291N11319"],   # Haiti — Hurricane Melissa (Oct 2025)
    "CERF-MOZ-24-RR-1438": ["2025068S15046"],   # Mozambique — TC Jude (Mar 2025)
    "CERF-PHL-25-RR-1485": ["2025308N10143"],   # Philippines — TC Fung-wong/Uwan (Nov 2025)
    "CERF-MDG-24-RR-1434": ["2026039S18057"],   # Madagascar — TC Gezani (Feb 2026)
    "CERF-MOZ-26-RR-1513": ["2026039S18057"],   # Mozambique — TC Gezani (Feb 2026, AA)
    "CERF-PHL-24-RR-1391": [                     # Philippines — six successive TCs, Oct–Nov 2024
        "2024293N13141",  # Trami
        "2024298N13150",  # Kong-rey
        "2024307N06143",  # Yinxing
        "2024312N14145",  # Toraji
        "2024314N07151",  # Usagi
        "2024313N10169",  # Man-yi (Pepito)
    ],
}

# code -> reason: storm allocation that is definitely NOT a tropical cyclone
NOT_TC = {
    "10-RR-COL-2904": "Colombia 2010 — La Niña rainy-season floods/landslides",
    "11-RR-ZWE-12668": "Zimbabwe 2011 — floods (and cholera), no TC",
    "07-RR-BGD-297": "Bangladesh Aug 2007 — monsoon floods (pre-Sidr), not a TC",
}

# Left unresolved on purpose:
#   06-UF-HTI-5791     Haiti Jun 2006 — pre-season underfunded allocation, unclear
#   CERF-SLB-26-RR-1540 Solomon Is — TC Maila (Apr 2026), not yet in IBTrACS
#   CERF-FSM-26-RR-1541 Micronesia — Typhoon Sinlaku (Apr 2026), not yet in IBTrACS


def main(write: bool):
    supp = load_supplemental()

    def keep(code):
        row = supp[supp["ApplicationCode"] == code]
        return row.iloc[0].to_dict() if not row.empty else {}

    print("=== SID matches ===")
    for code, sids in SID_FILLS.items():
        print(f"  {code:22s} -> {sids}")
        k = keep(code)
        supp = upsert_annotation(supp, code, {
            "sids": encode_sids(sids), "not_tc": None,
            "valid_month_start": k.get("valid_month_start"), "valid_year_start": k.get("valid_year_start"),
            "valid_month_end": k.get("valid_month_end"), "valid_year_end": k.get("valid_year_end"),
            "notes": k.get("notes"),
        })

    print("=== not_tc ===")
    for code, reason in NOT_TC.items():
        print(f"  {code:22s} -> {reason}")
        k = keep(code)
        supp = upsert_annotation(supp, code, {
            "sids": None, "not_tc": True,
            "valid_month_start": k.get("valid_month_start"), "valid_year_start": k.get("valid_year_start"),
            "valid_month_end": k.get("valid_month_end"), "valid_year_end": k.get("valid_year_end"),
            "notes": reason,
        })

    if not write:
        print("\n(dry run — pass --write to save)")
        return
    save_supplemental(supp)
    print(f"\nSaved. Blob now {len(supp)} rows.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    main(ap.parse_args().write)
