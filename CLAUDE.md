# ds-cerf-supplement — Claude guidance

## Architecture

Single-file Streamlit app (`app.py`). All data loading is in `src/`. No callbacks — Streamlit reruns the whole script on interaction.

## Key state management pattern

`baseline_df` in session state holds the exact DataFrame last passed to `st.data_editor`. Change detection compares the editor's returned DataFrame against `baseline_df`. Two things trigger a rebuild of `baseline_df` (and increment `editor_version` to reset the editor):

1. `st.session_state.needs_rebuild = True` — set after a successful save
2. `st.session_state.prev_filters` mismatch — set when sidebar filters change

Always use `.values` when building columns in the `edit_df` dict. Passing a pandas Series (with its own integer index) alongside `index=merged["ApplicationID"].values` causes pandas to index-align by label, producing all-None for any column not extracted with `.values`.

## Blob storage

`src/storage.py` targets container `global`, stage `dev`. Schema uses `valid_month_start`, `valid_year_start`, `valid_month_end`, `valid_year_end` (separate start/end years because a drought period can span a year boundary). The `load_supplemental` function adds any missing columns for schema migration safety.

## Storm lookup

`src/db.py` queries `storms.ibtracs_storms` (columns: `sid`, `name`, `season`). Displayed as `"NAME YEAR (SID)"`. `label_to_sid` dict is used to extract the raw SID when saving. Requires `PGSSLMODE=require` — set via `os.environ.setdefault` in `db.py`.

## Python version

Must use Python 3.12. `ocha-stratus` pulls in `psycopg2-binary` which doesn't build on Python 3.14 (removed `distutils`). Venv: `uv venv --python 3.12`.
