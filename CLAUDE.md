# ds-cerf-supplement — Claude guidance

## Architecture

Single-file Streamlit app (`app.py`). All data loading is in `src/`. No callbacks — Streamlit reruns the whole script on interaction.

## Key state management pattern

`baseline_df` in session state holds the exact DataFrame last passed to `st.data_editor`. Change detection compares the editor's returned DataFrame against `baseline_df`. Two things trigger a rebuild of `baseline_df` (and increment `editor_version` to reset the editor):

1. `st.session_state.needs_rebuild = True` — set after a successful save
2. `st.session_state.prev_filters` mismatch — set when sidebar filters change

Always use `.values` when building columns in the `edit_df` dict. Passing a pandas Series (with its own integer index) alongside `index=merged["ApplicationCode"].values` causes pandas to index-align by label, producing all-None for any column not extracted with `.values`.

## Blob storage

`src/storage.py` targets container `global`, stage `dev`. The `load_supplemental` function migrates older schemas in-memory (`sid`→`sids`) and adds any missing columns.

**Key column is `ApplicationCode`, NOT `ApplicationID`.** The CERF feed reuses `ApplicationID` across unrelated allocations (~431 collisions, e.g. ID 1019 = both Madagascar 2007 and Afghanistan 2023), which scrambles any join keyed on it. `ApplicationCode` (e.g. `23-RR-AFG-61441`) is unique and non-null for all 1609 rows — always key on it.

Storms are stored in `sids` as a JSON-encoded list of IBTrACS SIDs (`'["sid1","sid2"]'`) so one allocation can map to multiple storms (e.g. Haiti 2008 = Fay/Gustav/Hanna/Ike). Use `encode_sids`/`decode_sids` helpers. Drought period uses `valid_month_start`/`valid_year_start`/`valid_month_end`/`valid_year_end` (separate start/end years since a drought can span a year boundary).

`scripts/seed_from_existing.py` (rebuild SIDs from the tropicalcyclones CSV, with IBTrACS verification) and `scripts/fill_guessed_sids.py` (high-confidence guesses from allocation titles) both `--write` to the blob and are the source of truth for re-seeding.

## Storm editing UX (Streamlit constraint)

`st.data_editor` has no multi-select cell type. So the inline table edits only the **primary** storm (single searchable `SelectboxColumn`) plus a read-only "All storms" column; a separate **multi-storm editor** (`st.multiselect`) below the table assigns several storms to one allocation. Editing the inline primary only replaces the first SID unless cleared.

## Storm lookup

`src/db.py` queries `storms.ibtracs_storms` (columns: `sid`, `name`, `season`). Displayed as `"NAME YEAR (SID)"`. `label_to_sid` dict is used to extract the raw SID when saving. Requires `PGSSLMODE=require` — set via `os.environ.setdefault` in `db.py`.

## Python version

Must use Python 3.12. `ocha-stratus` pulls in `psycopg2-binary` which doesn't build on Python 3.14 (removed `distutils`). Venv: `uv venv --python 3.12`.
