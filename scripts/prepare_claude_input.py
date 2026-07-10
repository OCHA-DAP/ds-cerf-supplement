"""
Build claude_work/unresolved.json for the Claude matcher. Includes:
  * every unresolved in-scope (Rapid Response) storm allocation, and
  * any allocation with an open issue that has human comments — even if it's
    already matched — so review/correction replies can be acted on.

Each allocation carries candidate IBTrACS storms (±1 year), its current match
(if any), and any human comments left on its issue (authoritative).
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
from src.storage import decode_sids, is_resolved, load_supplemental  # noqa: E402
import scripts.check_storm_sids as chk  # noqa: E402

OUT = Path(__file__).parent.parent / "claude_work" / "unresolved.json"


def main():
    cerf = fetch_cerf_allocations.__wrapped__()
    storms = load_storms.__wrapped__().dropna(subset=["name"])
    storms = storms.assign(season=storms["season"].astype(int))
    sid_name = dict(zip(storms["sid"], storms["name"]))
    supp = load_supplemental()

    comments = chk.user_comments_by_code() if chk.TOKEN else {}
    sids_map = {r["ApplicationCode"]: decode_sids(r["sids"]) for _, r in supp.iterrows()} if not supp.empty else {}
    resolved = {r["ApplicationCode"] for _, r in supp.iterrows() if is_resolved(r)} if not supp.empty else set()

    cerf["_type"] = cerf["EmergencyTypeName"].map(classify_type)
    in_scope = cerf[(cerf["_type"] == "Storm") & (cerf["WindowFullName"] == "Rapid Response")]

    # unresolved ones + already-matched ones that have a human comment to act on
    include = set(in_scope[~in_scope["ApplicationCode"].isin(resolved)]["ApplicationCode"])
    include |= {c for c in comments if c in set(in_scope["ApplicationCode"])}

    rows = in_scope[in_scope["ApplicationCode"].isin(include)]
    allocations = []
    for _, a in rows.iterrows():
        code = a["ApplicationCode"]
        year = int(a["Year"]) if pd.notna(a["Year"]) else None
        cands = []
        if year is not None:
            near = storms[(storms["season"] - year).abs() <= 1]
            cands = [
                {"sid": r["sid"], "name": r["name"], "season": int(r["season"]),
                 "basin": r.get("genesis_basin")}
                for _, r in near.iterrows()
            ]
        current = sids_map.get(code, [])
        allocations.append({
            "code": code,
            "country": a["CountryName"],
            "year": year,
            "amount": float(a["TotalAmountApproved"]) if pd.notna(a["TotalAmountApproved"]) else None,
            "type": a["EmergencyTypeName"],
            "title": a["ApplicationTitle"],
            "summary": (a.get("CN_Summary") or "").strip()[:1500],
            "current_match": [{"sid": s, "name": sid_name.get(s)} for s in current],
            "user_comments": comments.get(code, []),
            "candidates": cands,
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"allocations": allocations}, indent=1))
    n_guided = sum(1 for x in allocations if x["user_comments"])
    print(f"Wrote {len(allocations)} allocations to {OUT} ({n_guided} with human comments)")


if __name__ == "__main__":
    main()
