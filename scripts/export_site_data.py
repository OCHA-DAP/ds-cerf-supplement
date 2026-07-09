"""
Generate site/data.json for the static GitHub Pages site: every storm CERF
allocation and the IBTrACS storm(s) it has been matched to.

Reads the supplemental blob + CERF API + IBTrACS names (DB). Run in CI before
deploying the Pages artifact.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

os.environ.setdefault("PGSSLMODE", "require")
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.cerf_api import classify_type, fetch_cerf_allocations  # noqa: E402
from src.db import load_storms  # noqa: E402
from src.storage import decode_sids, load_supplemental  # noqa: E402

OUT = Path(__file__).parent.parent / "site" / "data.json"


def main():
    cerf = fetch_cerf_allocations.__wrapped__()
    storms = load_storms.__wrapped__().dropna(subset=["name"])
    supp = load_supplemental()

    sid_info = {
        r["sid"]: {"name": r["name"], "season": int(r["season"])}
        for _, r in storms.iterrows()
    }
    sids_map, not_tc_map = {}, {}
    if not supp.empty:
        sids_map = {r["ApplicationCode"]: decode_sids(r["sids"]) for _, r in supp.iterrows()}
        not_tc_map = {r["ApplicationCode"]: bool(r.get("not_tc")) for _, r in supp.iterrows()}

    cerf["_type"] = cerf["EmergencyTypeName"].map(classify_type)

    rows = []
    for _, a in cerf.iterrows():
        code = a["ApplicationCode"]
        sids = sids_map.get(code, [])
        not_tc = not_tc_map.get(code, False)
        if a["_type"] != "Storm" and not sids and not not_tc:
            continue  # storm allocations, plus anything explicitly annotated
        storms_out = [
            {"sid": s, "name": sid_info.get(s, {}).get("name"),
             "season": sid_info.get(s, {}).get("season")}
            for s in sids
        ]
        status = "matched" if storms_out else ("not_tc" if not_tc else "unmatched")
        rows.append({
            "code": code,
            "country": a["CountryName"],
            "year": int(a["Year"]) if pd.notna(a["Year"]) else None,
            "amount": float(a["TotalAmountApproved"]) if pd.notna(a["TotalAmountApproved"]) else None,
            "type": a["EmergencyTypeName"],
            "title": a["ApplicationTitle"],
            "storms": storms_out,
            "status": status,
            "matched": bool(storms_out),
        })

    rows.sort(key=lambda r: (-(r["year"] or 0), r["country"] or ""))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": rows,
    }))
    n = lambda st: sum(1 for r in rows if r["status"] == st)
    print(f"Wrote {len(rows)} rows ({n('matched')} matched, "
          f"{n('not_tc')} not-a-TC, {n('unmatched')} unmatched) to {OUT}")


if __name__ == "__main__":
    main()
