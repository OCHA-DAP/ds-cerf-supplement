# ds-cerf-supplement — Claude guidance

## Architecture

No interactive app. Automated flows over the data in `src/`:

- **`check_storm_sids.py`** (daily GHA, 06:00) — backfills SIDs resolvable from titles, opens issues for the rest, and **auto-closes** issues once resolved (SID or `not_tc`).
- **`prepare_claude_input.py` → Claude Code → `apply_claude_matches.py`** (`claude-match-storms.yml`, daily 07:00) — Claude researches the remaining unresolved allocations (summary + web search) and writes matches; the apply step validates and writes only confidence ≥ 0.8. Claude gets Read/Write/WebSearch/WebFetch only — no blob/DB creds. Needs `CLAUDE_CODE_OAUTH_TOKEN` secret.
- **`export_site_data.py`** → `site/data.json`, served by static `site/index.html` on GitHub Pages (`deploy-site.yml`, no commits to `main`).

GitHub Pages source must be **GitHub Actions** (not a branch). `site/data.json` and `claude_work/` are git-ignored.

An allocation is "resolved" (dropped from all queues) when `is_resolved(row)` is true — it has a SID **or** `not_tc=True`. `not_tc` marks a storm allocation that is definitely not a tropical cyclone.

## Human-in-the-loop

Issues are the feedback channel. `check_storm_sids` opens `cerf-sid` issues for unresolved allocations and auto-closes them once resolved / out of scope. A human comment on an issue is **authoritative**: `prepare_claude_input` attaches issue comments (via `user_comments_by_code`, bot comments excluded) to each allocation, the prompt tells Claude to follow them, and `apply_claude_matches` writes the result and closes the issue.

Issues also carrying the **`review`** label are manual double-checks of an *existing* match (opened by `raise_review_issues.py`). The checker never auto-closes `review` issues, and `prepare` only feeds an already-matched allocation to Claude once it has a human comment — so a review issue sits until you reply "correct" / "it's actually X" / "not a TC", then gets updated and closed on the next run.

## Blob storage

`src/storage.py` targets container `global`, stage `dev`. The `load_supplemental` function migrates older schemas in-memory (`sid`→`sids`) and adds any missing columns.

**Key column is `ApplicationCode`, NOT `ApplicationID`.** The CERF feed reuses `ApplicationID` across unrelated allocations (~431 collisions, e.g. ID 1019 = both Madagascar 2007 and Afghanistan 2023), which scrambles any join keyed on it. `ApplicationCode` (e.g. `23-RR-AFG-61441`) is unique and non-null for all 1609 rows — always key on it.

Storms are stored in `sids` as a JSON-encoded list of IBTrACS SIDs (`'["sid1","sid2"]'`) so one allocation can map to multiple storms (e.g. Haiti 2008 = Fay/Gustav/Hanna/Ike). Use `encode_sids`/`decode_sids` helpers. Drought period uses `valid_month_start`/`valid_year_start`/`valid_month_end`/`valid_year_end` (separate start/end years since a drought can span a year boundary).

`scripts/seed_from_existing.py` (rebuild SIDs from the tropicalcyclones CSV, with IBTrACS verification) and `scripts/fill_guessed_sids.py` (high-confidence guesses from allocation titles) both `--write` to the blob — one-offs kept for re-seeding.

## CI install (important)

`pyproject` has `[tool.uv.sources]` pointing `ocha-stratus` at a local sibling path for dev. That path doesn't exist in CI, so all workflows install with `uv pip install --no-sources -e .` (pulls `ocha-stratus` from PyPI ≥0.1.7) and run with `uv run --no-sync`. Don't use `uv pip install --system` (conflicts with the setup-uv venv).

## Storm lookup

`src/db.py` queries `storms.ibtracs_storms` (columns: `sid`, `name`, `season`). Requires `PGSSLMODE=require` — set via `os.environ.setdefault` in `db.py`.

## Python version

Use Python 3.12. `ocha-stratus` pulls in `psycopg2-binary` which doesn't build on Python 3.14 (removed `distutils`). Venv: `uv venv --python 3.12`.
