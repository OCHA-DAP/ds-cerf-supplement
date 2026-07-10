"""
One-off: open `review` issues for existing matches I'm least confident about,
so they can be human-checked. These carry the `review` label so the daily
checker won't auto-close them; a comment (confirm / correct / not_tc) flows
through the Claude matcher on the next run.

Run with --write.
"""

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("PGSSLMODE", "require")
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.cerf_api import fetch_cerf_allocations  # noqa: E402
from src.db import load_storms  # noqa: E402
from src.storage import decode_sids, load_supplemental  # noqa: E402
import scripts.check_storm_sids as chk  # noqa: E402

# code -> why it's flagged for review
REVIEW = {
    "CERF-MOZ-26-RR-1513":
        "Anticipatory-action activation for **TC Gezani** (Feb 2026), but the "
        "summary says the cyclone veered away and did **not** make landfall in "
        "Mozambique. Confirm Gezani is the right match, or say if it should be unmatched.",
    "08-RR-HTI-5820":
        "Your original CSV tagged Haiti 2008 with **Ike** only; I expanded it to "
        "all four 2008 storms that hit Haiti (**Fay, Gustav, Hanna, Ike**). "
        "Confirm the set — all four, or just the triggering storm?",
    "08-RR-HTI-5831":
        "Your original CSV tagged Haiti 2008 with **Ike** only; I expanded it to "
        "all four 2008 storms (**Fay, Gustav, Hanna, Ike**). Confirm the set.",
    "08-RR-HTI-5837":
        "Your original CSV tagged Haiti 2008 with **Ike** only; I expanded it to "
        "all four 2008 storms (**Fay, Gustav, Hanna, Ike**). Confirm the set.",
    "09-RR-SLV-4818":
        "El Salvador, Nov 2009 — tagged **Hurricane Ida** (Atlantic/Caribbean), "
        "but El Salvador's disaster was largely driven by a Pacific low (96E). "
        "Confirm Ida is intended, or correct it.",
    "CERF-PHL-24-RR-1391":
        "Philippines Oct–Nov 2024 (reprogrammed AA→RR for six successive "
        "typhoons). I assigned all six (**Trami, Kong-rey, Yinxing, Toraji, "
        "Usagi, Man-yi**). Man-yi/Pepito is certain; confirm the full set.",
    "11-RR-GTM-5608":
        "Guatemala Oct 2011 — matched to unnamed **Tropical Depression 12-E** "
        "(SID `2011280N10268`), which has no name in IBTrACS. Confirm this is the "
        "intended system.",
}


def main(write: bool):
    cerf = fetch_cerf_allocations.__wrapped__().set_index("ApplicationCode")
    sid_name = dict(zip(load_storms.__wrapped__()["sid"], load_storms.__wrapped__()["name"]))
    supp = load_supplemental()
    sids_map = {r["ApplicationCode"]: decode_sids(r["sids"]) for _, r in supp.iterrows()}
    # skip only if an issue is currently OPEN (a prior closed one shouldn't block)
    open_codes = set(chk.open_issues_by_code()) if chk.TOKEN else set()

    if write and chk.TOKEN:
        chk.ensure_label()
        chk._gh("POST", f"/repos/{chk.REPO}/labels",
                json={"name": "review", "color": "d4a72c",
                      "description": "Existing match flagged for human review"})

    for code, reason in REVIEW.items():
        if code in open_codes:
            print(f"  skip (open issue exists) {code}")
            continue
        a = cerf.loc[code]
        cur = sids_map.get(code, [])
        cur_txt = ", ".join(f"{sid_name.get(s, s)} (`{s}`)" for s in cur) or "—"
        body = (
            f"⚠️ **Please double-check this existing match.** @{chk.ASSIGNEE}\n\n"
            f"- **Code:** `{code}`\n"
            f"- **Country / Year:** {a['CountryName']} {int(a['Year'])}\n"
            f"- **Title:** {a['ApplicationTitle']}\n"
            f"- **Currently matched to:** {cur_txt}\n\n"
            f"**Why flagged:** {reason}\n\n"
            f"**To resolve:** comment here — e.g. *\"correct\"*, *\"it's actually "
            f"Hurricane X\"*, *\"just Ike\"*, or *\"not a TC\"*. The daily matcher "
            f"reads your comment and updates the data, then closes this issue."
        )
        title = f"[CERF SID] review — {code} ({a['CountryName']} {int(a['Year'])})"
        print(f"  REVIEW {code}")
        if write and chk.TOKEN:
            r = chk._gh("POST", f"/repos/{chk.REPO}/issues",
                        json={"title": title, "body": body,
                              "labels": [chk.LABEL, "review"], "assignees": [chk.ASSIGNEE]})
            r.raise_for_status()
            print(f"         {r.json()['html_url']}")

    if not (write and chk.TOKEN):
        print("\n(dry run or no token — pass --write with GITHUB_TOKEN set)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    main(ap.parse_args().write)
