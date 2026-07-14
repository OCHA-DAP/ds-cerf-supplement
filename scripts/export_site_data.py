"""
Generate site/data.json for the static GitHub Pages site: every storm CERF
allocation and the IBTrACS storm(s) it has been matched to, plus every drought
allocation and its valid (meteorological drought / rainfall-deficit) period.

Reads the supplemental tables + CERF API + IBTrACS names (DB). Run in CI before
deploying the Pages artifact. Rows carry kind: "storm" | "drought" (an
allocation annotated with both appears once per kind).
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
from src.storage import decode_sids, has_valid_period, load_supplemental  # noqa: E402

OUT = Path(__file__).parent.parent / "site" / "data.json"


def base_row(a) -> dict:
    return {
        "code": a["ApplicationCode"],
        "country": a["CountryName"],
        "year": int(a["Year"]) if pd.notna(a["Year"]) else None,
        "amount": float(a["TotalAmountApproved"]) if pd.notna(a["TotalAmountApproved"]) else None,
        "type": a["EmergencyTypeName"],
        "title": a["ApplicationTitle"],
    }


def main():
    cerf = fetch_cerf_allocations.__wrapped__()
    storms = load_storms.__wrapped__().dropna(subset=["name"])
    supp = load_supplemental()

    sid_info = {
        r["sid"]: {"name": r["name"], "season": int(r["season"])}
        for _, r in storms.iterrows()
    }
    supp_by_code = {r["ApplicationCode"]: r for _, r in supp.iterrows()} if not supp.empty else {}

    def _i(v):
        return int(v) if pd.notna(v) else None

    cerf["_type"] = cerf["EmergencyTypeName"].map(classify_type)

    rows = []
    for _, a in cerf.iterrows():
        code = a["ApplicationCode"]
        # Rapid Response only — Underfunded Emergencies are out of scope
        if a["WindowFullName"] != "Rapid Response":
            continue
        s = supp_by_code.get(code)
        sids = decode_sids(s["sids"]) if s is not None else []
        not_tc = bool(s.get("not_tc")) if s is not None and pd.notna(s.get("not_tc")) else False
        dated = s is not None and has_valid_period(s)

        # ---- storm row: storm allocations, plus anything storm-annotated
        if a["_type"] == "Storm" or sids or not_tc:
            storms_out = [
                {"sid": x, "name": sid_info.get(x, {}).get("name"),
                 "season": sid_info.get(x, {}).get("season")}
                for x in sids
            ]
            status = "matched" if storms_out else ("not_tc" if not_tc else "unmatched")
            rows.append({**base_row(a), "kind": "storm", "storms": storms_out,
                         "status": status, "matched": bool(storms_out)})

        # ---- drought row: drought allocations, plus anything with a valid period
        if a["_type"] == "Drought" or dated:
            valid = None
            if dated:
                valid = {"ms": _i(s["valid_month_start"]), "ys": _i(s["valid_year_start"]),
                         "me": _i(s["valid_month_end"]), "ye": _i(s["valid_year_end"])}
            conf = s.get("confidence") if s is not None else None
            rows.append({**base_row(a), "kind": "drought", "valid": valid,
                         "confidence": float(conf) if conf is not None and pd.notna(conf) else None,
                         "notes": (s.get("notes") or None) if s is not None else None,
                         "status": "dated" if dated else "needs_period"})

    rows.sort(key=lambda r: (-(r["year"] or 0), r["country"] or ""))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": rows,
    }))
    n = lambda st: sum(1 for r in rows if r["status"] == st)
    print(f"Wrote {len(rows)} rows ({n('matched')} matched, {n('not_tc')} not-a-TC, "
          f"{n('unmatched')} unmatched storms; {n('dated')} dated, "
          f"{n('needs_period')} undated droughts) to {OUT}")


if __name__ == "__main__":
    main()
