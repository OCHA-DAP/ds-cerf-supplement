"""
Apply claude_work/drought_matches.json (produced by the Claude drought step),
then manage the cerf-drought issues.

Each match: {code, valid_month_start, valid_year_start, valid_month_end,
             valid_year_end, confidence, reasoning}

Rules:
  * confidence >= THRESHOLD and validated -> write the valid (meteorological
    drought) period to aa.cerf_supplement, with the confidence and the
    reasoning as notes (any storm sids/not_tc on the row are preserved);
  * not_drought: true (and no period) at confidence >= NOT_DROUGHT_THRESHOLD ->
    flag the allocation as not a meteorological drought (mis-typed in the feed:
    food-price crisis, conflict, displacement...). Deliberately a HIGHER bar —
    the flag is a terminal state, used conservatively;
  * otherwise -> leave undated; an issue is opened (label cerf-drought) with
    Claude's suggestion for a human to confirm.

Validation: months 1–12, end not before start, span <= 24 months, and the
period within 2 years of the allocation year (the deficit usually precedes the
allocation by up to a year; anticipatory allocations can precede the deficit).

Issue management mirrors check_storm_sids: open an issue for every still-undated
in-scope allocation (unless one already exists in any state), auto-close issues
whose allocation is now dated or out of scope.
"""

import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

os.environ.setdefault("PGSSLMODE", "require")
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.cerf_api import classify_type, fetch_cerf_allocations  # noqa: E402
from src.storage import (  # noqa: E402
    ensure_tables, is_drought_resolved, load_supplemental, save_supplemental,
    upsert_annotation,
)
import scripts.check_storm_sids as chk  # noqa: E402

THRESHOLD = 0.8
NOT_DROUGHT_THRESHOLD = 0.9   # terminal flag — deliberately a higher bar
LABEL = "cerf-drought"
MATCHES = Path(__file__).parent.parent / "claude_work" / "drought_matches.json"

MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt_period(ms, ys, me, ye) -> str:
    return f"{MONTHS[ms]} {ys} – {MONTHS[me]} {ye}"


def validate(m: dict, alloc_year: int | None) -> str | None:
    """Return a rejection reason, or None when the period is valid."""
    try:
        ms, ys = int(m["valid_month_start"]), int(m["valid_year_start"])
        me, ye = int(m["valid_month_end"]), int(m["valid_year_end"])
    except (KeyError, TypeError, ValueError):
        return "missing/non-integer period fields"
    if not (1 <= ms <= 12 and 1 <= me <= 12):
        return "month out of range"
    span = (ye - ys) * 12 + (me - ms) + 1
    if span < 1:
        return "end before start"
    if span > 24:
        return f"span {span} months > 24"
    if alloc_year and (abs(ys - alloc_year) > 2 or abs(ye - alloc_year) > 2):
        return f"period {ys}–{ye} too far from allocation year {alloc_year}"
    return None


def build_issue_body(alloc, suggestion: dict | None) -> str:
    amount = alloc["TotalAmountApproved"]
    amount_s = f"${float(amount):,.0f}" if pd.notna(amount) else "—"
    summary = alloc.get("CN_Summary")
    summary = (summary if isinstance(summary, str) else "").strip()
    summary = (summary[:600] + "…") if len(summary) > 600 else summary

    parts = [
        f"This drought allocation has no **valid period** (the months of the "
        f"actual rainfall deficit) assigned yet, and it could not be dated "
        f"automatically with high confidence. @{chk.ASSIGNEE} please confirm "
        f"the period.",
        "",
        "### Allocation",
        f"- **Code:** `{alloc['ApplicationCode']}`",
        f"- **CERF page:** {chk.cerf_url(alloc['ApplicationCode'], alloc['Year'])}",
        f"- **Country:** {alloc['CountryName']}",
        f"- **Year:** {alloc['Year']}",
        f"- **Amount:** {amount_s}",
        f"- **Title:** {alloc['ApplicationTitle']}",
    ]
    if summary:
        parts += ["", f"> {summary}"]
    if suggestion:
        why = (suggestion.get("reasoning") or "").strip()
        if suggestion.get("not_drought"):
            period = "**not a meteorological drought**"
        elif validate(suggestion, None) is None:
            period = fmt_period(int(suggestion["valid_month_start"]),
                                int(suggestion["valid_year_start"]),
                                int(suggestion["valid_month_end"]),
                                int(suggestion["valid_year_end"]))
        else:
            period = "—"
        conf = float(suggestion.get("confidence", 0) or 0)
        parts += ["", "### 🤖 Claude suggestion (not auto-applied)",
                  f"- **Period:** {period} (confidence {conf:.0%})"]
        if why:
            parts.append(f"- {why}")
    parts += [
        "",
        "### How to resolve",
        "Reply with the rainfall-deficit period in plain language — e.g. "
        "_\"Oct 2020 – Mar 2021\"_, _\"the failed 2016 deyr\"_, or "
        "_\"suggestion is correct\"_. If there was no meteorological drought "
        "behind this allocation (mis-typed in the feed — food-price crisis, "
        "conflict, displacement), reply _\"not a drought\"_. The next daily "
        "run reads your reply as authoritative, applies it, and closes this "
        "issue.",
    ]
    return "\n".join(parts)


def main():
    matches = json.loads(MATCHES.read_text()) if MATCHES.exists() else []
    if not MATCHES.exists():
        print(f"No matches file at {MATCHES}; running issue management only.")

    ensure_tables()  # also migrates in the confidence column
    cerf = fetch_cerf_allocations.__wrapped__()
    cerf["_type"] = cerf["EmergencyTypeName"].map(classify_type)
    in_scope = cerf[(cerf["_type"] == "Drought")
                    & (cerf["WindowFullName"] == "Rapid Response")]
    by_code = in_scope.set_index("ApplicationCode")
    supp = load_supplemental()
    open_issues = chk.open_issues_by_code(label=LABEL) if chk.TOKEN else {}

    applied, skipped, suggestions = 0, 0, {}
    for m in matches:
        code = m.get("code")
        if code not in by_code.index:
            print(f"  skip (not an in-scope drought allocation) {code}"); continue
        year = int(by_code.loc[code, "Year"]) if pd.notna(by_code.loc[code, "Year"]) else None
        conf = float(m.get("confidence", 0) or 0)
        reason = (m.get("reasoning") or "").strip()
        not_drought = bool(m.get("not_drought"))
        problem = validate(m, year)

        if not_drought:
            has_period = any(m.get(k) is not None for k in (
                "valid_month_start", "valid_year_start",
                "valid_month_end", "valid_year_end"))
            if conf >= NOT_DROUGHT_THRESHOLD and not has_period:
                _prev = supp[supp["ApplicationCode"] == code]
                prev = _prev.iloc[0] if len(_prev) else {}
                supp = upsert_annotation(supp, code, {
                    "sids": prev.get("sids"), "not_tc": prev.get("not_tc"),
                    "not_drought": True,
                    "valid_month_start": None, "valid_year_start": None,
                    "valid_month_end": None, "valid_year_end": None,
                    "confidence": conf, "notes": reason or None,
                })
                applied += 1
                print(f"  APPLY {code:22s} conf={conf:.2f} NOT a meteorological drought")
                if chk.TOKEN and code in open_issues:
                    chk.close_issue(open_issues[code],
                                    f"✅ flagged not a meteorological drought "
                                    f"(confidence {conf:.0%}).\n\n{reason}")
            else:
                skipped += 1
                suggestions[code] = m
                why = ("not_drought carries a period" if has_period
                       else f"not_drought needs confidence >= {NOT_DROUGHT_THRESHOLD}")
                print(f"  SKIP  {code:22s} ({why})")
            continue

        if conf >= THRESHOLD and problem is None:
            ms, ys = int(m["valid_month_start"]), int(m["valid_year_start"])
            me, ye = int(m["valid_month_end"]), int(m["valid_year_end"])
            # preserve any storm annotation already on the row; a real period
            # supersedes a not_drought flag
            _prev = supp[supp["ApplicationCode"] == code]
            prev = _prev.iloc[0] if len(_prev) else {}
            supp = upsert_annotation(supp, code, {
                "sids": prev.get("sids"), "not_tc": prev.get("not_tc"),
                "not_drought": None,
                "valid_month_start": ms, "valid_year_start": ys,
                "valid_month_end": me, "valid_year_end": ye,
                "confidence": conf, "notes": reason or None,
            })
            applied += 1
            print(f"  APPLY {code:22s} conf={conf:.2f} {fmt_period(ms, ys, me, ye)}")
            if chk.TOKEN and code in open_issues:
                chk.close_issue(open_issues[code],
                                f"✅ valid period set to {fmt_period(ms, ys, me, ye)} "
                                f"(confidence {conf:.0%}).\n\n{reason}")
        else:
            skipped += 1
            suggestions[code] = m
            why = problem or f"confidence {conf:.2f} < {THRESHOLD}"
            print(f"  SKIP  {code:22s} ({why})")

    if applied:
        save_supplemental(supp)

    # ---------------------------------------------------------------- issues
    created = closed = 0
    if chk.TOKEN:
        chk.ensure_label(LABEL, color="bf8700",
                         description="CERF drought allocation needs its valid (rainfall-deficit) period")
        supp = load_supplemental() if applied else supp
        dated = ({r["ApplicationCode"] for _, r in supp.iterrows() if is_drought_resolved(r)}
                 if not supp.empty else set())
        undated = in_scope[~in_scope["ApplicationCode"].isin(dated)]
        seen = chk.existing_issue_codes(label=LABEL)

        for _, alloc in undated.iterrows():
            code = alloc["ApplicationCode"]
            if code in seen:
                # existing open issue: post the low-confidence suggestion once
                if code in open_issues and code in suggestions:
                    num = open_issues[code]
                    existing = chk._gh("GET", f"/repos/{chk.REPO}/issues/{num}/comments",
                                       params={"per_page": 100}).json()
                    if not any(c["body"].lstrip().startswith("🤖") for c in existing):
                        s = suggestions[code]
                        conf = float(s.get("confidence", 0) or 0)
                        chk._gh("POST", f"/repos/{chk.REPO}/issues/{num}/comments",
                                json={"body": f"🤖 **Claude suggestion** (confidence "
                                              f"{conf:.0%}, not auto-applied):\n\n"
                                              f"{(s.get('reasoning') or '').strip()}"})
                continue
            title = f"[CERF drought] {code} — {alloc['CountryName']} {alloc['Year']}"
            url = chk.create_issue(title, build_issue_body(alloc, suggestions.get(code)),
                                   label=LABEL)
            print(f"  FLAG {code:22s} issue: {url}")
            created += 1
            time.sleep(2)  # stay under GitHub's secondary rate limit on creation

        # close issues whose allocation is now dated (or out of scope)
        keep_open = set(undated["ApplicationCode"])
        for issue in chk._issues("open", LABEL):
            m = chk.CODE_RE.search(issue.get("title", ""))
            if m and m.group(1) not in keep_open:
                chk.close_issue(issue["number"],
                                "✅ Closing automatically — this allocation now has its "
                                "valid period, is flagged not-a-drought, or is out of scope.")
                closed += 1

    summary = (f"Drought matcher: {applied} applied, {skipped} left for review, "
               f"{created} issues opened, {closed} closed")
    print(summary)
    if (s := os.getenv("GITHUB_STEP_SUMMARY")):
        Path(s).write_text(f"### {summary}\n")


if __name__ == "__main__":
    main()
