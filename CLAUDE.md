# ds-cerf-supplement — Claude guidance

## Architecture

No interactive app. Two automated flows over the data in `src/`:

- **`scripts/check_storm_sids.py`** (daily GHA) — backfills SIDs resolvable from allocation titles, opens GitHub issues for the rest.
- **`scripts/export_site_data.py`** → `site/data.json`, served by the static `site/index.html` page on GitHub Pages (deployed via `deploy-site.yml`, no commits to `main`).

GitHub Pages source must be **GitHub Actions** (not a branch). `site/data.json` is git-ignored and regenerated on each deploy.

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
