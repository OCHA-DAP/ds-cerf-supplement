# ds-cerf-supplement — Claude guidance

## Architecture

No interactive app. A daily chained pipeline over the data in `src/`, run as ONE
Databricks job, **CERF Supplement Daily** (`databricks.yml`, 05:30 UTC; four
chained tasks on one job cluster, each running the unchanged scripts through
`databricks/run_task.py`). It replaced four `workflow_run`-chained GitHub Actions
workflows in 2026-09 when GitHub runners lost network access to the DB — only
`deploy-site.yml` remains on GitHub, and it never touches the DB:

```
refresh  →  match_storms (deterministic → Claude)  →  match_droughts (Claude)  →  publish_site (data.json → dev blob → dispatch deploy-site.yml)
```

The Claude research steps run the Claude Code CLI headlessly on the cluster
(`scripts/run_claude.py`, installed at run time, `CLAUDE_CODE_OAUTH_TOKEN` from
the dsci scope, `DSCI_*` stripped from its env). GitHub issues are reached with
the dsci secret `CERF_SUPPLEMENT_GH_TOKEN` (exposed as `GITHUB_TOKEN`). Neither
secret is a `spark_env_vars` reference — the wrapper resolves them with dbutils
so a missing key fails the task, not the cluster launch.

- **`refresh_mirror.py`** (task `refresh`, daily 05:30) — upserts the OneGMS feed into `aa.cerf_allocation` (feed columns + deterministic `aa_keyword`), keyed on `ApplicationCode`. **Sole writer of the table** (a pure mirror — the KB's AA layer lives in separate tables: `aa.actual_activation` + the curated `aa.activation_allocation` crosswalk, maintained by the KB's `aa-links` confirm flow). This is the upstream of every matcher.
- **`refresh_projects.py`** (second step of task `refresh`) — mirrors the **project-level** feed (`v1/project/All.json`, ~8.6k agency projects, ~18 MB, takes ~8 min to generate server-side) into `aa.cerf_project` + `aa.cerf_project_sector` + `aa.cerf_project_country`, joining to the allocation mirror on `application_code`. Sole writer of all three. Same ID gotcha as allocations: `projectID` has ~3.1k collisions — **key on `projectCode`**. Project amounts sum exactly to the allocation's `amount_approved`. No downstream consumer in this repo (the matchers don't read it); it exists so project-level CERF facts (agency splits, sector amounts, planned/reached demographics, HRP cap-codes) are queryable beside the allocations. Multi-row chunked inserts — plain executemany is one round-trip per row and unusably slow against the Azure DB.
- **`refresh_cbpf.py`** (third step of task `refresh`) — mirrors **CBPF/regional-fund allocations** from the public CBPF OData API (`cbpfapi.unocha.org/vo2/odata`, `AllocationTypes` + `MstPooledFund`) into `aa.cbpf_allocation` + `aa.cbpf_fund`, and (re)creates the fund-agnostic union view `aa.v_allocation` over both allocation mirrors. Sole writer of all three. A CBPF *allocation* = a titled Standard/Reserve envelope containing a set of approved projects. **Key on `(PooledFundId, AllocationTypeId)` — `AllocationTypeId` alone has ~50 cross-fund collisions** (the CBPF cousin of the ApplicationID gotcha). The feed also lists CERF allocations (`FundTypeId=2`) — excluded; the CERF feed stays authoritative for CERF. `aa_keyword` uses the same title/summary convention (plus French variants). This repo is the home of ALL OneGMS mirrors — add future OneGMS-sourced mirrors here.
- **`refresh_cbpf_projects.py`** (fourth step of task `refresh`) — mirrors CBPF **project-level** data (`ProjectSummary`, fetched per pooled fund — the unfiltered endpoint times out; one feed row per project × cluster × admin-location, normalized here) into `aa.cbpf_project` (one row per project = a grant to one implementing partner, keyed `chf_project_code`, joins to `aa.cbpf_allocation` via `(pooled_fund_id, allocation_type_id)`) + `aa.cbpf_project_cluster` (sector splits) + `aa.cbpf_project_subip` (sub-implementing partners — the CBPF sub-grant layer). Admin-location splits deliberately not mirrored. Sole writer of all three; full-replace load, chunked inserts.
- **`refresh_contributions.py`** (fifth step of task `refresh`) — mirrors **donor contributions** to the funds, the base for donor shares of AA (a donor's share of a fund's income in a fiscal year × the AA the fund released / pre-arranged that year): `aa.cerf_contribution` from `cerfgms-webapi.unocha.org/v1/donorcontribution.json` (one row per contribution, keyed `contributionCode`; the pledge / commitment / received / write-off legs are nested lists in the feed and are summed here — `received_usd` is the cash-in-year base; upsert) and `aa.cbpf_contribution` from the CBPF OData `ContributionTotal` set (fund × donor × fiscal year, paid + pledged; the feed repeats ~12 keys, summed at load with `n_feed_rows`; `pooled_fund_id` resolved by name against `MstPooledFund`; full replace). Sole writer of both; (re)creates `aa.v_contribution`, the fund-agnostic union with donor names harmonized across the two feeds (`donor` vs `donor_raw`; e.g. CBPF "United States" = CERF "United States of America", CERF "Korea" = "Republic of Korea", the UNF private buckets collapse into one). CBPF `ContributionTotal` is the published figure (it matches the annual-report query); the contribution-level vo1 set disagrees for 2023–24 and is not used.
- **`check_storm_sids.py`** (task `match_storms`, deterministic step) — backfills SIDs resolvable from titles, opens issues for the rest, **auto-closes** issues once resolved (SID or `not_tc`).
- **`prepare_claude_input.py` → Claude Code → `apply_claude_matches.py`** (task `match_storms`, after the deterministic step) — Claude researches the remaining unresolved allocations (summary + web search) and writes matches; the apply step validates and writes only confidence ≥ 0.8. Claude gets Read/Write/WebSearch/WebFetch only — no DB creds. Model input is `claude-sonnet-5` (must be a *current* id — Claude API ids drift). Needs the `CLAUDE_CODE_OAUTH_TOKEN` dsci secret.
- **`prepare_drought_input.py` → Claude Code → `apply_drought_matches.py`** (task `match_droughts`) — same shape for droughts, no deterministic stage: Claude dates each undated RR drought allocation's **valid period** (the rainfall-deficit months — often up to a year before the allocation, per the OneGMS narratives + web search); apply validates (months 1–12, end ≥ start, span ≤ 24 months, within 2 years of the allocation year), writes confidence ≥ 0.8 with confidence+reasoning on the row, and opens `cerf-drought` issues for the rest (human replies authoritative next run).
- **`export_site_data.py`** → `site/data.json` (rows carry `kind: storm|drought`); **`publish_site_data.py`** uploads it to the dev blob and dispatches `deploy-site.yml`, which fetches it (`fetch_site_data.py`) and serves it with the static `site/index.html` (Storms/Droughts tabs) on GitHub Pages (no commits to `main`).

**Matchers chain, they don't fan out**: both write `aa.cerf_supplement` via a
transactional full-replace, so `match_droughts` `depends_on` `match_storms`
(and `publish_site` on `match_droughts`). Add another matcher as a new task
chained off the last matcher, and move `publish_site`'s dependency to it. The
job pulls `main` at run time (`source: GIT`): code changes ship on merge, a
`databricks bundle deploy -t prod -p DEFAULT` is only needed when the job
config changes.

GitHub Pages source must be **GitHub Actions** (not a branch). `site/data.json` and `claude_work/` are git-ignored. `deploy-site.yml` gets `data.json` from the dev blob (`src/site_blob.py`), where `publish_site` left it.

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

`pyproject` has `[tool.uv.sources]` pointing `ocha-stratus` at a local sibling path for dev. That path doesn't exist in CI, so `deploy-site.yml` installs with `uv pip install --no-sources -e .` (pulls `ocha-stratus` from PyPI ≥0.1.7) and runs with `uv run --no-sync`. Don't use `uv pip install --system` (conflicts with the setup-uv venv). On Databricks the task libraries in `databricks.yml` mirror the pyproject dependencies; the wrapper puts the repo root on `PYTHONPATH` instead of installing the package.

## Storm lookup

`src/db.py` queries `storms.ibtracs_storms` (columns: `sid`, `name`, `season`). Requires `PGSSLMODE=require` — set via `os.environ.setdefault` in `db.py`.

## Python version

Use Python 3.12. `ocha-stratus` pulls in `psycopg2-binary` which doesn't build on Python 3.14 (removed `distutils`). Venv: `uv venv --python 3.12`.
