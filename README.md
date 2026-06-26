# ds-cerf-supplement

Streamlit app for supplementing CERF allocation data with storm and drought metadata.

## What it does

Pulls all CERF allocations from the [OneGMS API](https://cerfgms-webapi.unocha.org/v1/application/All.xml) and provides an editable table to add:

- **Storm allocations** — one or more IBTrACS SIDs, looked up from `storms.ibtracs_storms` in the DB and displayed as `STORM NAME YEAR (SID)`. An allocation can map to multiple storms (e.g. Haiti 2008 = Fay/Gustav/Hanna/Ike).
- **Drought allocations** — start/end month and year of the rainfall deficit period

Supplemental data is stored as a parquet file in blob storage, keyed by CERF `ApplicationCode`. Any allocation type can be annotated regardless of its classified emergency type (to handle mis-classifications).

## Blob storage

- Container: `global`, stage: `dev`
- Path: `cerf/cerf_supplemental_data.parquet`
- Schema: `ApplicationCode` (unique key), `sids` (JSON list of IBTrACS SIDs — supports multiple storms), `valid_month_start`, `valid_year_start`, `valid_month_end`, `valid_year_end`, `notes`, `updated_at`
- Keyed on `ApplicationCode`, **not** `ApplicationID` — the CERF feed reuses `ApplicationID` across unrelated allocations.

## Setup

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e .
cp .env.example .env  # fill in env vars
streamlit run app.py
```

Required env vars (see `.env.example`):

| Variable | Purpose |
|---|---|
| `DSCI_AZ_BLOB_DEV_SAS` | Read access to dev blob |
| `DSCI_AZ_BLOB_DEV_SAS_WRITE` | Write access to dev blob |
| `PGSSLMODE` | Set to `require` for Azure Postgres (or add to `.env`) |

The DB connection uses `ocha_stratus.get_engine()` — standard OCHA stratus setup applies.

## Usage

- **Filter** by emergency type (all types from CERF data) and annotation status (All / Needs annotation / Annotated)
- **Edit** the primary Storm, Start Month, Start Year, End Month, End Year, or Notes directly in the table
- **Multiple storms** — use the multi-storm editor below the table (pick an allocation, multi-select storms). The inline Storm column edits only the primary storm.
- **Save** — click the "Save N changes" button that appears when edits are detected; only changed rows are written
- **Refresh** — button in the sidebar re-fetches CERF data and storm list (both cached for 1h / 24h respectively)
- Clearing all editable fields for a row removes that row from the supplemental data

## Daily storm-SID check (GitHub Actions)

`.github/workflows/check-storm-sids.yml` runs daily (and on demand via the
Actions tab → *Check storm SIDs* → *Run workflow*). It:

1. Finds every storm allocation with no SID assigned yet (backfill — covers all history).
2. Parses the storm name(s) from the allocation title and resolves them against `storms.ibtracs_storms`.
3. **Backfills** the SID(s) when every named storm resolves to exactly one IBTrACS storm within ±1 year (handles multi-storm titles like "TC Batsirai & Emnati").
4. **Opens a GitHub issue** (label `cerf-sid`, assigned to `@t-downing`) for any it can't resolve — no storm name in the title, a name not in IBTrACS, or an ambiguous match — with candidate storms and research links. Already-issued allocations (open or closed) are skipped, so it won't spam or re-open resolved ones.

Run `python scripts/check_storm_sids.py --dry-run` locally to preview without writing.

Required repo **secrets** (Settings → Secrets and variables → Actions):
`DSCI_AZ_BLOB_DEV_SAS`, `DSCI_AZ_BLOB_DEV_SAS_WRITE`, `DSCI_AZ_DB_DEV_HOST`,
`DSCI_AZ_DB_DEV_UID`, `DSCI_AZ_DB_DEV_PW`. (`GITHUB_TOKEN` is provided automatically.)

## Project structure

```
app.py                  # Streamlit app (single entry point)
src/
  cerf_api.py           # Fetch + parse OneGMS XML → DataFrame
  db.py                 # Load storms from storms.ibtracs_storms
  storage.py            # Read/write supplemental parquet via ocha-stratus
scripts/
  seed_from_existing.py # One-off: rebuild SIDs from the tropicalcyclones CSV
  fill_guessed_sids.py  # One-off: high-confidence SID guesses from titles
  check_storm_sids.py   # Daily: backfill resolvable SIDs, open issues for the rest
```
