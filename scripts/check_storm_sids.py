"""
Daily check: find storm CERF allocations with no SID assigned yet, try to
resolve the storm(s) from the allocation title against IBTrACS, and either

  * BACKFILL the SID(s) when every named storm resolves unambiguously, or
  * open a GitHub issue (tagging the maintainer) listing what was found and
    helpful research links, so the storm can be picked manually.

Idempotent: an allocation that already has a SID, or that already has an issue
(open or closed) for it, is skipped — so it won't fill the same thing twice or
re-open issues you've dealt with.

Run locally with --dry-run to preview. The GitHub Action runs it with --write.
"""

import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd
import requests

os.environ.setdefault("PGSSLMODE", "require")
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.cerf_api import classify_type, fetch_cerf_allocations  # noqa: E402
from src.db import load_storms  # noqa: E402
from src.storage import (  # noqa: E402
    decode_sids,
    encode_sids,
    load_supplemental,
    save_supplemental,
    upsert_annotation,
)

ASSIGNEE = os.getenv("CERF_ISSUE_ASSIGNEE", "t-downing")
LABEL = "cerf-sid"
REPO = os.getenv("GITHUB_REPOSITORY", "OCHA-DAP/ds-cerf-supplement")
TOKEN = os.getenv("GITHUB_TOKEN")

_KW = r"tropical cyclone|tropical storm|cyclone|hurricane|typhoon|storm|tc"
_NAME = r"[A-Z][a-zA-Z]+"
_CONN = r"\s*(?:,|&|/|\band\b)\s*"
# a storm keyword followed by one or more Capitalised names joined by and/&/,/ —
# e.g. "Cyclone Idai", "TC Batsirai and Emnati", "TC Batsirai & TC Emnati"
STORM_SEQ_RE = re.compile(
    rf"\b(?:{_KW})\b\s+({_NAME}(?:{_CONN}(?:(?:{_KW})\s+)?{_NAME})*)",
    re.IGNORECASE,
)
# tokens that match the name pattern but are keywords / not storm names
_EXCLUDE = {
    "TC", "STORM", "STORMS", "CYCLONE", "CYCLONES", "HURRICANE", "TYPHOON",
    "TROPICAL", "SEASON", "EMERGENCY", "APPLICATION", "AND",
}


def parse_storm_names(title: str) -> list[str]:
    names, seen = [], set()
    for m in STORM_SEQ_RE.finditer(title or ""):
        for tok in re.findall(_NAME, m.group(1)):
            key = tok.upper()
            if key in _EXCLUDE or key in seen:
                continue
            seen.add(key)
            names.append(tok)
    return names


# ---------------------------------------------------------------- GitHub API
def _gh(method: str, path: str, **kw):
    r = requests.request(
        method,
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
        },
        timeout=60,
        **kw,
    )
    return r


def existing_issue_codes() -> set[str]:
    """ApplicationCodes that already have an issue (any state) under our label."""
    codes, page = set(), 1
    while True:
        r = _gh("GET", f"/repos/{REPO}/issues",
                params={"labels": LABEL, "state": "all", "per_page": 100, "page": page})
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for issue in batch:
            m = re.search(r"\b(\d{2}-[A-Z]{2,3}-[A-Z]{3}-\d+|CERF-[A-Z]{3}-\d{2}-[A-Z]{2}-\d+)\b",
                          issue.get("title", ""))
            if m:
                codes.add(m.group(1))
        page += 1
    return codes


def ensure_label():
    _gh("POST", f"/repos/{REPO}/labels",
        json={"name": LABEL, "color": "1f6feb",
              "description": "CERF allocation needs a storm SID assigned"})


def create_issue(title: str, body: str):
    r = _gh("POST", f"/repos/{REPO}/issues",
            json={"title": title, "body": body, "labels": [LABEL], "assignees": [ASSIGNEE]})
    r.raise_for_status()
    return r.json()["html_url"]


# ---------------------------------------------------------------- issue body
def research_links(names: list[str], country: str, year) -> str:
    lines = []
    for name in names or ["tropical cyclone"]:
        q = quote_plus(f"{name} {year} cyclone {country}")
        lines.append(f"- [Wikipedia: {name} {year}](https://en.wikipedia.org/w/index.php?search={q})")
        lines.append(f"- [Google: {name} {year} {country}](https://www.google.com/search?q={q})")
    lines.append("- [IBTrACS browser](https://ncics.org/ibtracs/index.php?name=browse-name)")
    return "\n".join(lines)


def candidate_table(names: list[str], by_name: dict) -> str:
    rows = []
    for name in names:
        for sid, season, basin in by_name.get(name.upper(), []):
            rows.append(f"| {name} | {season} | `{sid}` | {basin} |")
    if not rows:
        return "_No IBTrACS storms found matching the name(s) in the title._"
    return "| Name | Season | SID | Basin |\n|---|---|---|---|\n" + "\n".join(rows)


def build_body(alloc, names, problems, resolved, by_name) -> str:
    amount = alloc["TotalAmountApproved"]
    amount_s = f"${float(amount):,.0f}" if pd.notna(amount) else "—"
    summary = (alloc.get("CN_Summary") or "").strip()
    summary = (summary[:500] + "…") if len(summary) > 500 else summary

    parts = [
        f"This storm allocation has no IBTrACS SID assigned and could not be "
        f"resolved automatically. @{ASSIGNEE} please pick the storm(s).",
        "",
        "### Allocation",
        f"- **Code:** `{alloc['ApplicationCode']}`",
        f"- **Country:** {alloc['CountryName']}",
        f"- **Year:** {alloc['Year']}",
        f"- **Amount:** {amount_s}",
        f"- **Emergency type:** {alloc['EmergencyTypeName']}",
        f"- **Title:** {alloc['ApplicationTitle']}",
    ]
    if summary:
        parts += ["", f"> {summary}"]

    parts += ["", "### Why it was flagged"]
    if not names:
        parts.append("- No storm name found in the allocation title.")
    for name, reason, _ in problems:
        parts.append(f"- **{name}**: {reason}.")
    if resolved:
        sugg = ", ".join(f"{n} → `{s}`" for n, s in resolved.items())
        parts.append(f"- Confident matches (others unresolved): {sugg}")

    parts += [
        "",
        "### Candidate IBTrACS storms",
        candidate_table(names, by_name),
        "",
        "### Research links",
        research_links(names, alloc["CountryName"], alloc["Year"]),
        "",
        "### How to resolve",
        "Open the **CERF Supplement** app, find this allocation, and assign the "
        "storm(s) (use the multi-storm editor if more than one). Then close this "
        "issue. If there's genuinely no IBTrACS storm (e.g. a tornado or a "
        "not-yet-archived storm), just close the issue — it won't be re-opened.",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------- main
def main(write: bool):
    cerf = fetch_cerf_allocations.__wrapped__()
    storms = load_storms.__wrapped__().dropna(subset=["name"])
    supp = load_supplemental()

    by_name: dict[str, list] = {}
    for _, s in storms.iterrows():
        by_name.setdefault(s["name"].upper(), []).append(
            (s["sid"], int(s["season"]), s.get("genesis_basin"))
        )

    have_sid = set()
    if not supp.empty:
        have_sid = {r["ApplicationCode"] for _, r in supp.iterrows() if decode_sids(r["sids"])}

    cerf["_type"] = cerf["EmergencyTypeName"].map(classify_type)
    storm_allocs = cerf[(cerf["_type"] == "Storm") & (~cerf["ApplicationCode"].isin(have_sid))]
    print(f"{len(storm_allocs)} storm allocations without a SID")

    seen_issue_codes = existing_issue_codes() if (write and TOKEN) else set()
    if write and TOKEN:
        ensure_label()

    filled, to_flag = [], []
    for _, alloc in storm_allocs.iterrows():
        year = int(alloc["Year"]) if pd.notna(alloc["Year"]) else None
        names = parse_storm_names(alloc["ApplicationTitle"])
        resolved, problems = {}, []
        for name in names:
            cands = by_name.get(name.upper(), [])
            near = [(sid, ssn, b) for sid, ssn, b in cands if year and abs(ssn - year) <= 1]
            if len(near) == 1:
                resolved[name] = near[0][0]
            elif not near:
                problems.append((name, "no IBTrACS match within ±1 year", cands))
            else:
                problems.append((name, f"{len(near)} IBTrACS matches within ±1 year", near))

        if names and not problems:
            filled.append((alloc, list(resolved.values()), resolved))
        else:
            to_flag.append((alloc, names, problems, resolved))

    print(f"  → {len(filled)} can be backfilled, {len(to_flag)} need manual review")

    # --- backfill confident matches ---
    if filled:
        for alloc, sids, resolved in filled:
            print(f"  FILL {alloc['ApplicationCode']:22s} {list(resolved.items())}")
            if write:
                supp = upsert_annotation(supp, alloc["ApplicationCode"], {
                    "sids": encode_sids(sids),
                    "valid_month_start": None, "valid_year_start": None,
                    "valid_month_end": None, "valid_year_end": None, "notes": None,
                })
        if write:
            save_supplemental(supp)
            print(f"  saved {len(filled)} backfilled SIDs to blob")

    # --- open issues for the rest ---
    created = 0
    for alloc, names, problems, resolved in to_flag:
        code = alloc["ApplicationCode"]
        if code in seen_issue_codes:
            print(f"  skip (issue exists) {code}")
            continue
        title = f"[CERF SID] {code} — {alloc['CountryName']} {alloc['Year']}"
        print(f"  FLAG {code:22s} names={names or '∅'}")
        if write and TOKEN:
            url = create_issue(title, build_body(alloc, names, problems, resolved, by_name))
            print(f"       issue: {url}")
            created += 1

    summary = (f"Storm SID check: {len(filled)} backfilled, "
               f"{created if (write and TOKEN) else len(to_flag)} flagged for review")
    print(summary)
    if (gh_summary := os.getenv("GITHUB_STEP_SUMMARY")):
        with open(gh_summary, "a") as f:
            f.write(f"### {summary}\n")

    if not write:
        print("\n(dry run — pass --write to backfill the blob and open issues)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--write", action="store_true", help="backfill blob + open issues")
    g.add_argument("--dry-run", action="store_true", help="preview only (default)")
    main(ap.parse_args().write)
