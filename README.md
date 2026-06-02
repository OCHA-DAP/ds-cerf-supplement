# ds-cerf-supplement

Streamlit app for supplementing CERF allocation data with storm and drought metadata.

## What it does

Pulls all CERF allocations from the [OneGMS API](https://cerfgms-webapi.unocha.org/v1/application/All.xml) and provides an editable table to add:

- **Storm allocations** — IBTrACS SID, looked up from `storms.ibtracs_storms` in the DB and displayed as `STORM NAME YEAR (SID)`
- **Drought allocations** — start/end month and year of the rainfall deficit period

Supplemental data is stored as a parquet file in blob storage, keyed by CERF `ApplicationID`. Any allocation type can be annotated regardless of its classified emergency type (to handle mis-classifications).

## Blob storage

- Container: `global`, stage: `dev`
- Path: `cerf/cerf_supplemental_data.parquet`
- Schema: `ApplicationID`, `sid`, `valid_month_start`, `valid_year_start`, `valid_month_end`, `valid_year_end`, `notes`, `updated_at`

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
- **Edit** Storm, Start Month, Start Year, End Month, End Year, or Notes directly in the table
- **Save** — click the "Save N changes" button that appears when edits are detected; only changed rows are written
- **Refresh** — button in the sidebar re-fetches CERF data and storm list (both cached for 1h / 24h respectively)
- Clearing all editable fields for a row removes that row from the supplemental data

## Project structure

```
app.py                  # Streamlit app (single entry point)
src/
  cerf_api.py           # Fetch + parse OneGMS XML → DataFrame
  db.py                 # Load storms from storms.ibtracs_storms
  storage.py            # Read/write supplemental parquet via ocha-stratus
```
