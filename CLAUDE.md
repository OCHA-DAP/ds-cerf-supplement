# ds-cerf-supplement — Claude guidance

## Architecture

No interactive app. A daily chained pipeline over the data in `src/`. Only
`refresh-mirror` is scheduled (cron 05:30 UTC); the rest fire in order via
`workflow_run` so they always run against a freshly-mirrored feed:

```
Refresh OneGMS mirror  →  Match storms (deterministic → Claude)  →  Match droughts (Claude)  →  Deploy site
```

- **`refresh_mirror.py`** (`refresh-mirror.yml`, daily 05:30) — upserts the OneGMS feed into `aa.cerf_allocation` (feed columns + deterministic `aa_keyword`), keyed on `ApplicationCode`. **Sole writer of the table** (a pure mirror — the KB's AA layer lives in separate tables: `aa.actual_activation` + the curated `aa.activation_allocation` crosswalk, maintained by the KB's `aa-links` confirm flow). This is the upstream of every matcher.
- **`refresh_projects.py`** (second step of `refresh-mirror.yml`) — mirrors the **project-level** feed (`v1/project/All.json`, ~8.6k agency projects, ~18 MB, takes ~8 min to generate server-side) into `aa.cerf_project` + `aa.cerf_project_sector` + `aa.cerf_project_country`, joining to the allocation mirror on `application_code`. Sole writer of all three. Same ID gotcha as allocations: `projectID` has ~3.1k collisions — **key on `projectCode`**. Project amounts sum exactly to the allocation's `amount_approved`. No downstream consumer in this repo (the matchers don't read it); it exists so project-level CERF facts (agency splits, sector amounts, planned/reached demographics, HRP cap-codes) are queryable beside the allocations. Multi-row chunked inserts — plain executemany is one round-trip per row and unusably slow against the Azure DB.
- **`refresh_cbpf.py`** (third step of `refresh-mirror.yml`) — mirrors **CBPF/regional-fund allocations** from the public CBPF OData API (`cbpfapi.unocha.org/vo2/odata`, `AllocationTypes` + `MstPooledFund`) into `aa.cbpf_allocation` + `aa.cbpf_fund`, and (re)creates the fund-agnostic union view `aa.v_allocation` over both allocation mirrors. Sole writer of all three. A CBPF *allocation* = a titled Standard/Reserve envelope containing a set of approved projects. **Key on `(PooledFundId, AllocationTypeId)` — `AllocationTypeId` alone has ~50 cross-fund collisions** (the CBPF cousin of the ApplicationID gotcha). The feed also lists CERF allocations (`FundTypeId=2`) — excluded; the CERF feed stays authoritative for CERF. `aa_keyword` uses the same title/summary convention (plus French variants). This repo is the home of ALL OneGMS mirrors — add future OneGMS-sourced mirrors here.
- **`refresh_cbpf_projects.py`** (fourth step of `refresh-mirror.yml`) — mirrors CBPF **project-level** data (`ProjectSummary`, fetched per pooled fund — the unfiltered endpoint times out; one feed row per project × cluster × admin-location, normalized here) into `aa.cbpf_project` (one row per project = a grant to one implementing partner, keyed `chf_project_code`, joins to `aa.cbpf_allocation` via `(pooled_fund_id, allocation_type_id)`) + `aa.cbpf_project_cluster` (sector splits) + `aa.cbpf_project_subip` (sub-implementing partners — the CBPF sub-grant layer). Admin-location splits deliberately not mirrored. Sole writer of all three; full-replace load, chunked inserts.
- **`refresh_cbpf_full.py`** (`refresh-cbpf-full.yml`, its own daily cron 03:00 UTC — NOT part of `refresh-mirror.yml`, which must stay short because the matchers chain off it) — the **complete raw mirror of the public CBPF API** into dev schema **`cbpf`** (~70 tables). Registry-driven: every table is a `TableSpec` in `src/cbpf_registry.py` (source, fan-out, params, declared key, ERD group, join-by-convention edges, one-line description); `src/cbpf_mirror.py` fetches (per-fund fan-out over the 34 distinct `PFAbbrv` where the API demands it), types columns (vo3/vo1 `$metadata` for entity sets, value inference for stored queries), snake_cases names, and **full-replaces each table in its own transaction** (one failing table ≠ failed run; the run exits 1 at the end if any failed). Every table carries `fetched_at`; every run appends a row per table to **`cbpf.mirror_run`** — the hook for the planned monthly snapshotting. Upstream column drift is absorbed with `ALTER TABLE … ADD COLUMN`. `src/cbpf_api.py` (OneGMS OData: entity sets + `GlobalGenericDataExtract` stored queries) and `src/bdt_api.py` (Beneficiary Data Tool) are the clients. Covers: 28 vo3 entity sets, the 9 vo1 sets that still answer, the 33 public stored queries, 6 BDT tables. **API gotchas baked in** (all verified live 2026-09-18): stored queries must be fetched as **CSV** — the JSON form silently drops columns (PF_PROJ_SUMMARY_V4: 30 vs 68); `ShowAllPooledFunds=1` does NOT widen a fund-scoped query (per-fund fan-out is the only way); most fund-scoped calls are an HTML 500 without a fund; a secured SPCode on the public host is an HTTP **200** `{"Error": …}` row; blank `AllocationYear(s)` = all years; French-locale text can be double-encoded UTF-8 (repaired). The `aa.cbpf_*` tables are **unchanged** — they stay the normalized AA-facing subset with their own scripts/consumers. `--dry-run --funds SUD15 --only <tables>` for testing; `--list` prints the registry.
- **`export_cbpf_erd.py`** (deploy-site step) → `site/mirror/meta.json` (git-ignored): registry + live DB introspection (columns, row counts, last `mirror_run`) for **`site/mirror/index.html`**, the CBPF mirror ERD page (Mermaid, rendered client-side; Core / All / By-group views; the `aa.cbpf_*` tables are hand-described in the script). Nested under the existing root page (linked from its header; back-link at the top) — the root product's URL was deliberately not moved.
- **`check_storm_sids.py`** (`match-storms.yml` job 1) — backfills SIDs resolvable from titles, opens issues for the rest, **auto-closes** issues once resolved (SID or `not_tc`).
- **`prepare_claude_input.py` → Claude Code → `apply_claude_matches.py`** (`match-storms.yml` job 2, `needs` job 1) — Claude researches the remaining unresolved allocations (summary + web search) and writes matches; the apply step validates and writes only confidence ≥ 0.8. Claude gets Read/Write/WebSearch/WebFetch only — no DB creds. Model input is `claude-sonnet-5` (must be a *current* id — Claude API ids drift). Needs `CLAUDE_CODE_OAUTH_TOKEN` secret.
- **`prepare_drought_input.py` → Claude Code → `apply_drought_matches.py`** (`match-drought.yml`) — same shape for droughts, no deterministic stage: Claude dates each undated RR drought allocation's **valid period** (the rainfall-deficit months — often up to a year before the allocation, per the OneGMS narratives + web search); apply validates (months 1–12, end ≥ start, span ≤ 24 months, within 2 years of the allocation year), writes confidence ≥ 0.8 with confidence+reasoning on the row, and opens `cerf-drought` issues for the rest (human replies authoritative next run).
- **`export_site_data.py`** → `site/data.json` (rows carry `kind: storm|drought`), served by static `site/index.html` (Storms/Droughts tabs) on GitHub Pages (`deploy-site.yml`, no commits to `main`).

**Matchers chain, they don't fan out**: both write `aa.cerf_supplement` via a
transactional full-replace, so `match-drought` triggers on `workflow_run` of
"Match storms" (and deploy-site on "Match droughts"). Add another matcher as a
new workflow chained off the last matcher, and move deploy-site's trigger to it.
`workflow_run` chains only fire when the workflow file is on the **default
branch** — merge to `main` to activate.

GitHub Pages source must be **GitHub Actions** (not a branch). `site/data.json` and `claude_work/` are git-ignored.

An allocation is "resolved" (dropped from all queues) when `is_resolved(row)` is true — it has a SID **or** `not_tc=True`. `not_tc` marks a storm allocation that is definitely not a tropical cyclone. The drought equivalent is `has_valid_period(row)` — all four `valid_*` fields set.

## Human-in-the-loop

Issues are the feedback channel. `check_storm_sids` opens `cerf-sid` issues for unresolved allocations and auto-closes them once resolved / out of scope; `apply_drought_matches` does the same with `cerf-drought` issues for undated drought allocations (Claude's low-confidence suggestion included in the body). A human comment on an issue is **authoritative**: the prepare scripts attach issue comments (via `user_comments_by_code(label=...)`, bot comments excluded) to each allocation, the prompts tell Claude to follow them, and the apply scripts write the result and close the issue. The issue helpers live in `check_storm_sids.py` and take a `label=` param.

Issues also carrying the **`review`** label are manual double-checks of an *existing* match (opened by `raise_review_issues.py`). The checker never auto-closes `review` issues, and `prepare` only feeds an already-matched allocation to Claude once it has a human comment — so a review issue sits until you reply "correct" / "it's actually X" / "not a TC", then gets updated and closed on the next run.

## Storage — dev DB, schema `aa`

Source of truth is the **DB** (was blob parquet until 2026-07; migrated via `scripts/migrate_blob_to_db.py`, blob now retired). Two normalized tables in the KB-owned `aa` schema, beside `aa.cerf_allocation`:
- `aa.cerf_allocation_storm(application_code, sid)` — one row per matched storm
- `aa.cerf_supplement(application_code, not_tc, valid_month_*, valid_year_*, confidence, notes, updated_at)` — `confidence` is the Claude matcher's stated confidence for auto-applied picks (NULL = human-set); `ensure_tables()` adds the column to pre-existing tables

`src/storage.py` keeps the **same public API + DataFrame shape** as the old blob code (`load_supplemental`/`save_supplemental`/`upsert_annotation`/`remove_annotation`, `sids` column is a JSON list string) — only the backing store changed, so the checker/export/prepare callers are unchanged. `save_supplemental` does a transactional full-replace of both tables (fine — small, single-writer). Writers need `get_engine(write=True)` (DSCI_AZ_DB_DEV_*_WRITE creds); readers use the read engine.

**Key column is `ApplicationCode`, NOT `ApplicationID`.** The CERF feed reuses `ApplicationID` across unrelated allocations (~431 collisions, e.g. ID 1019 = both Madagascar 2007 and Afghanistan 2023). `ApplicationCode` (e.g. `23-RR-AFG-61441`) is unique — always key on it.

Storms use `encode_sids`/`decode_sids` (JSON list ↔ rows in cerf_allocation_storm) so one allocation can map to multiple storms. Drought uses `valid_month_start`/`valid_year_start`/`valid_month_end`/`valid_year_end` (separate start/end years — a drought can span a year boundary).

`scripts/seed_from_existing.py` (rebuild SIDs from the tropicalcyclones CSV, with IBTrACS verification) and `scripts/fill_guessed_sids.py` (high-confidence guesses from allocation titles) both `--write` to the blob — one-offs kept for re-seeding.

## CI install (important)

`pyproject` has `[tool.uv.sources]` pointing `ocha-stratus` at a local sibling path for dev. That path doesn't exist in CI, so all workflows install with `uv pip install --no-sources -e .` (pulls `ocha-stratus` from PyPI ≥0.1.7) and run with `uv run --no-sync`. Don't use `uv pip install --system` (conflicts with the setup-uv venv).

## Storm lookup

`src/db.py` queries `storms.ibtracs_storms` (columns: `sid`, `name`, `season`). Requires `PGSSLMODE=require` — set via `os.environ.setdefault` in `db.py`.

## Python version

Use Python 3.12. `ocha-stratus` pulls in `psycopg2-binary` which doesn't build on Python 3.14 (removed `distutils`). Venv: `uv venv --python 3.12`.
