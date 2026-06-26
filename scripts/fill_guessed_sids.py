"""
Fill high-confidence SID guesses for storm allocations whose CERF title names
a single storm that resolves to exactly one IBTrACS SID with a matching season.

Multi-storm titles (e.g. "Batsirai & Emnati"), anticipatory-action allocations,
non-tropical events (tornado), and storms not yet in IBTrACS are intentionally
left blank for manual research.

Run with --write to commit.
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

os.environ.setdefault("PGSSLMODE", "require")
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.cerf_api import fetch_cerf_allocations  # noqa: E402
from src.db import load_storms  # noqa: E402
from src.storage import (  # noqa: E402
    BLOB_NAME,
    CONTAINER,
    STAGE,
    encode_sids,
    load_supplemental,
    save_supplemental,
)

# ApplicationCode -> (sid, storm label). All verified present in IBTrACS with
# season within 1 yr of the allocation year.
HIGH_CONFIDENCE = {
    "19-RR-MWI-35650": ("2019063S18038", "Idai 2019"),
    "22-RR-MOZ-52564": ("2022065S16055", "Gombe 2022"),
    "23-RR-BGD-59459": ("2023129N08091", "Mocha 2023"),
    "23-RR-MWI-58010": ("2023036S12117", "Freddy 2023"),
    "23-RR-MOZ-57965": ("2023036S12117", "Freddy 2023"),
    "23-RR-MMR-59095": ("2023129N08091", "Mocha 2023"),
    "23-RR-VUT-61859": ("2023292S03172", "Lola 2023"),
    "24-RR-BGD-63521": ("2024145N14087", "Remal 2024"),
    "CERF-CUB-24-RR-1432": ("2024309N13283", "Rafael 2024"),
    "CERF-CUB-24-RR-1430": ("2024293N21294", "Oscar 2024"),
    "CERF-GRD-24-RR-1393": ("2024181N09320", "Beryl 2024"),
    "CERF-JAM-24-RR-1392": ("2024181N09320", "Beryl 2024"),
    "24-RR-MDG-64484": ("2024084S12054", "Gamane 2024"),
    "CERF-MOZ-24-RR-1440": ("2024345S11062", "Chido 2024"),
    "CERF-PHL-24-RR-1431": ("2024293N13141", "Trami 2024"),
    "CERF-CUB-25-RR-1495": ("2025291N11319", "Melissa 2025"),
    "CERF-JAM-25-RR-1494": ("2025291N11319", "Melissa 2025"),
    "CERF-MDG-26-RR-1518": ("2026030S16043", "Fytia 2026"),
}


def main(write: bool):
    cerf = fetch_cerf_allocations.__wrapped__()
    valid_codes = set(cerf["ApplicationCode"])
    storms = load_storms.__wrapped__().set_index("sid")

    rows = []
    print("=== High-confidence fills ===")
    for code, (sid, label) in HIGH_CONFIDENCE.items():
        assert code in valid_codes, f"ApplicationCode not found: {code}"
        assert sid in storms.index, f"SID not in IBTrACS: {sid} ({label})"
        rows.append({"ApplicationCode": code, "sids": encode_sids([sid])})
        print(f"  {code:22s} -> {sid}  ({label})")

    new_rows = pd.DataFrame(rows)
    new_rows["valid_month_start"] = None
    new_rows["valid_year_start"] = None
    new_rows["valid_month_end"] = None
    new_rows["valid_year_end"] = None
    new_rows["notes"] = None
    new_rows["updated_at"] = datetime.now(timezone.utc)

    existing = load_supplemental()
    already = set(existing["ApplicationCode"]) if not existing.empty else set()
    to_add = new_rows[~new_rows["ApplicationCode"].isin(already)]
    print(f"\n{len(new_rows)} guesses, {len(to_add)} new (rest already present)")

    if not write:
        print("(dry run — pass --write to save)")
        return

    out = pd.concat([existing, to_add], ignore_index=True)
    save_supplemental(out)
    print(f"Saved. Blob now {len(out)} rows ({CONTAINER}/{BLOB_NAME}, stage={STAGE})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    main(ap.parse_args().write)
