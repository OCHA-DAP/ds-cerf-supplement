"""
Build claude_work/unresolved.json: the storm allocations that still have no
SID and aren't flagged not_tc, each with candidate IBTrACS storms (±1 year of
the allocation year) for Claude to choose from.
"""

import json
import os
import sys
from pathlib import Path

import pandas as pd

os.environ.setdefault("PGSSLMODE", "require")
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.cerf_api import classify_type, fetch_cerf_allocations  # noqa: E402
from src.db import load_storms  # noqa: E402
from src.storage import is_resolved, load_supplemental  # noqa: E402

OUT = Path(__file__).parent.parent / "claude_work" / "unresolved.json"


def main():
    cerf = fetch_cerf_allocations.__wrapped__()
    storms = load_storms.__wrapped__().dropna(subset=["name"])
    supp = load_supplemental()

    resolved = {r["ApplicationCode"] for _, r in supp.iterrows() if is_resolved(r)} if not supp.empty else set()
    cerf["_type"] = cerf["EmergencyTypeName"].map(classify_type)
    un = cerf[
        (cerf["_type"] == "Storm")
        & (cerf["WindowFullName"] == "Rapid Response")  # exclude Underfunded
        & (~cerf["ApplicationCode"].isin(resolved))
    ]

    storms = storms.assign(season=storms["season"].astype(int))
    allocations = []
    for _, a in un.iterrows():
        year = int(a["Year"]) if pd.notna(a["Year"]) else None
        cands = []
        if year is not None:
            near = storms[(storms["season"] - year).abs() <= 1]
            cands = [
                {"sid": r["sid"], "name": r["name"], "season": int(r["season"]),
                 "basin": r.get("genesis_basin")}
                for _, r in near.iterrows()
            ]
        allocations.append({
            "code": a["ApplicationCode"],
            "country": a["CountryName"],
            "year": year,
            "amount": float(a["TotalAmountApproved"]) if pd.notna(a["TotalAmountApproved"]) else None,
            "type": a["EmergencyTypeName"],
            "title": a["ApplicationTitle"],
            "summary": (a.get("CN_Summary") or "").strip()[:1500],
            "candidates": cands,
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"allocations": allocations}, indent=1))
    print(f"Wrote {len(allocations)} unresolved allocations to {OUT}")


if __name__ == "__main__":
    main()
