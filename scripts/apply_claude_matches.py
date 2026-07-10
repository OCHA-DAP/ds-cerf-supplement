"""
Apply claude_work/matches.json (produced by the Claude step).

Each match: {code, sids: [...], not_tc: bool, confidence: float, reasoning: str}

Rules:
  * confidence >= THRESHOLD and validated  -> write to blob (SID(s) or not_tc)
  * otherwise                              -> leave for the GitHub issue; if an
                                              open issue exists, post Claude's
                                              suggestion as a comment.

SID validation: every SID must exist in storms.ibtracs_storms and its season
must be within ±1 year of the allocation year.
"""

import json
import os
import sys
from pathlib import Path

import pandas as pd

os.environ.setdefault("PGSSLMODE", "require")
sys.path.insert(0, str(Path(__file__).parent.parent))
import ocha_stratus as stratus  # noqa: E402

from src.cerf_api import fetch_cerf_allocations  # noqa: E402
from src.storage import (  # noqa: E402
    encode_sids, load_supplemental, save_supplemental, upsert_annotation,
)
import scripts.check_storm_sids as chk  # noqa: E402

THRESHOLD = 0.8
MATCHES = Path(__file__).parent.parent / "claude_work" / "matches.json"


def all_storm_seasons() -> dict[str, int]:
    with stratus.get_engine().connect() as conn:
        df = pd.read_sql("SELECT sid, season FROM storms.ibtracs_storms", conn)
    return {r["sid"]: int(r["season"]) for _, r in df.iterrows()}


def main():
    if not MATCHES.exists():
        print(f"No matches file at {MATCHES}; nothing to apply.")
        return
    matches = json.loads(MATCHES.read_text())
    seasons = all_storm_seasons()
    cerf = fetch_cerf_allocations.__wrapped__().set_index("ApplicationCode")
    supp = load_supplemental()
    open_issues = chk.open_issues_by_code() if chk.TOKEN else {}

    applied, skipped = 0, 0
    for m in matches:
        code = m.get("code")
        if code not in cerf.index:
            print(f"  skip (unknown code) {code}"); continue
        year = int(cerf.loc[code, "Year"]) if pd.notna(cerf.loc[code, "Year"]) else None
        conf = float(m.get("confidence", 0) or 0)
        sids = [s for s in (m.get("sids") or []) if s]
        not_tc = bool(m.get("not_tc"))
        reason = (m.get("reasoning") or "").strip()

        # validate SIDs
        bad = [s for s in sids if s not in seasons or (year and abs(seasons[s] - year) > 1)]
        valid = sids and not bad

        if conf >= THRESHOLD and (valid or (not_tc and not sids)):
            flagged_not_tc = not_tc and not sids
            supp = upsert_annotation(supp, code, {
                "sids": encode_sids(sids), "not_tc": flagged_not_tc or None,
                "valid_month_start": None, "valid_year_start": None,
                "valid_month_end": None, "valid_year_end": None, "notes": None,
            })
            applied += 1
            print(f"  APPLY {code:22s} conf={conf:.2f} sids={sids} not_tc={flagged_not_tc}")
            # close the issue right away with a confirmation
            if chk.TOKEN and code in open_issues:
                what = "flagged not-a-TC" if flagged_not_tc else f"assigned {', '.join(sids)}"
                chk.close_issue(open_issues[code],
                                f"✅ {what} (confidence {conf:.0%}).\n\n{reason}")
        else:
            skipped += 1
            why = "bad SID(s)" if bad else ("low confidence" if conf < THRESHOLD else "no decision")
            print(f"  SKIP  {code:22s} conf={conf:.2f} ({why})")
            if chk.TOKEN and code in open_issues and reason:
                sugg = (f"🤖 **Claude suggestion** (confidence {conf:.0%}, not "
                        f"auto-applied): sids={sids or '—'}, not_tc={not_tc}\n\n{reason}")
                chk._gh("POST", f"/repos/{chk.REPO}/issues/{open_issues[code]}/comments",
                        json={"body": sugg})

    if applied:
        save_supplemental(supp)
    print(f"Applied {applied}, skipped {skipped}.")
    if (s := os.getenv("GITHUB_STEP_SUMMARY")):
        Path(s).write_text(f"### Claude matcher: {applied} applied, {skipped} left for review\n")


if __name__ == "__main__":
    main()
