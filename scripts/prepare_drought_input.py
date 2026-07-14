"""
Build claude_work/unresolved_droughts.json for the Claude drought matcher. Includes:
  * every in-scope (Rapid Response) drought allocation with no valid period yet, and
  * any drought allocation with an open cerf-drought issue that has human comments —
    even if already dated — so review/correction replies can be acted on.

The "valid period" is the METEOROLOGICAL drought — the months of the actual
rainfall deficit (failed rainy season(s)), which often precedes the allocation by
up to a year (and, for anticipatory allocations, can extend past it). Each
allocation carries the OneGMS narratives (summary/overview/rationale — usually
naming the failed seasons), its current period (if any), and any human comments
left on its issue (authoritative).
"""

import json
import os
import sys
from pathlib import Path

import pandas as pd

os.environ.setdefault("PGSSLMODE", "require")
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.cerf_api import (  # noqa: E402
    classify_type, fetch_cerf_allocations, fetch_project_titles,
)
from src.storage import is_drought_resolved, load_supplemental  # noqa: E402
import scripts.check_storm_sids as chk  # noqa: E402

LABEL = "cerf-drought"
OUT = Path(__file__).parent.parent / "claude_work" / "unresolved_droughts.json"


def _clean(v):
    """None for missing values — pandas hands back NaN (a float) for empty feed fields."""
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else v


def _clip(v, n: int) -> str:
    v = _clean(v)
    return "" if v is None else str(v).strip()[:n]


def main():
    cerf = fetch_cerf_allocations.__wrapped__()
    supp = load_supplemental()

    comments = chk.user_comments_by_code(label=LABEL) if chk.TOKEN else {}
    open_issues = chk.open_issues_by_code(label=LABEL) if chk.TOKEN else {}
    supp_by_code = {r["ApplicationCode"]: r for _, r in supp.iterrows()} if not supp.empty else {}
    dated = {c for c, r in supp_by_code.items() if is_drought_resolved(r)}

    cerf["_type"] = cerf["EmergencyTypeName"].map(classify_type)
    in_scope = cerf[(cerf["_type"] == "Drought") & (cerf["WindowFullName"] == "Rapid Response")]

    # undated ones + already-dated ones that have a human comment to act on.
    # An undated allocation whose issue is already open but has NO human reply is
    # excluded: the suggestion has been posted, there's nothing new to research
    # until a human weighs in (keeps the daily Claude workload proportional to
    # new allocations + replies, not the whole backlog).
    include = set(in_scope[~in_scope["ApplicationCode"].isin(dated)]["ApplicationCode"])
    include -= {c for c in open_issues if c not in comments}
    include |= {c for c in comments if c in set(in_scope["ApplicationCode"])}

    rows = in_scope[in_scope["ApplicationCode"].isin(include)]
    allocations = []
    for _, a in rows.iterrows():
        code = a["ApplicationCode"]
        # project titles from the cerf.un.org page — often the only text naming
        # the driver/season when the feed narratives are empty (pre-2013)
        projects = fetch_project_titles(code, a["Year"])
        s = supp_by_code.get(code)
        current = None
        if s is not None and has_valid_period(s):
            current = {
                "valid_month_start": int(s["valid_month_start"]),
                "valid_year_start": int(s["valid_year_start"]),
                "valid_month_end": int(s["valid_month_end"]),
                "valid_year_end": int(s["valid_year_end"]),
                "notes": s.get("notes"),
            }
        allocations.append({
            "code": code,
            "country": a["CountryName"],
            "year": int(a["Year"]) if pd.notna(a["Year"]) else None,
            "amount": float(a["TotalAmountApproved"]) if pd.notna(a["TotalAmountApproved"]) else None,
            "type": a["EmergencyTypeName"],
            "title": a["ApplicationTitle"],
            "endorsement_date": _clean(a.get("CN_ERC_EndorsementDate")),
            "summary": _clip(a.get("CN_Summary"), 2000),
            "overview": _clip(a.get("OverviewoftheHumanitarianSituation"), 1200),
            "rationale": _clip(a.get("RationaleforCERFAllocation"), 1200),
            "projects": projects,
            "current_period": current,
            "user_comments": comments.get(code, []),
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"allocations": allocations}, indent=1))
    n_guided = sum(1 for x in allocations if x["user_comments"])
    print(f"Wrote {len(allocations)} drought allocations to {OUT} ({n_guided} with human comments)")


if __name__ == "__main__":
    main()
