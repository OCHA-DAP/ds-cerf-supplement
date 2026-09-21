# ds-cerf-supplement

Supplements CERF allocation data with the IBTrACS storm(s) each storm allocation
relates to and the **valid period** (the months of the actual meteorological
drought) of each drought allocation, and publishes the results as a static site
on GitHub Pages.

A daily pipeline (no interactive app), chained so each stage runs after the last:

1. **Refresh OneGMS mirror** ([workflow](.github/workflows/refresh-mirror.yml)) — daily 05:30 UTC. Upserts the CERF feed into `aa.cerf_allocation` so newly-published allocations are matchable. On success it triggers the matchers.
2. **Match storms** ([workflow](.github/workflows/match-storms.yml)) — runs after the mirror. Stage 1 backfills SIDs that resolve unambiguously from the allocation title and opens a GitHub issue (tagging the maintainer) for the rest; stage 2 (Claude) researches the remainder and applies validated, high-confidence matches.
3. **Match droughts** ([workflow](.github/workflows/match-drought.yml)) — runs after the storm matcher (matchers serialize — they share `aa.cerf_supplement` writes). Claude reads each undated drought allocation's OneGMS narratives (+ web search) to date the **rainfall deficit** — often up to a year before the allocation — and the apply step writes only validated, confidence ≥ 0.8 periods; the rest get a `cerf-drought` issue to confirm.
4. **Deploy site** ([workflow](.github/workflows/deploy-site.yml)) — runs after matching. Deploys `site/` to GitHub Pages: a **landing page** at the root (team landing-page convention, HDX v2 styling) with each product under its own path — the Storms / Droughts review page at **https://ocha-dap.github.io/ds-cerf-supplement/review/** (regenerated from the DB) and the CBPF mirror ERD at **https://ocha-dap.github.io/ds-cerf-supplement/mirror/**.

Annotations live in the **dev DB** (schema `aa`), next to the `aa.cerf_allocation`
feed mirror. SIDs are added by the matcher; anything it can't resolve is filled by
Claude or by a human reply on the issue, keyed by `ApplicationCode`.

## Storage (dev DB, schema `aa`)

Source of truth is the DB (not a blob) — the matches are mutable, row-level
records, and they join to the rest of the AA data:

- `aa.cerf_allocation_storm (application_code, sid)` — one row per matched storm (multi-storm friendly, e.g. Haiti 2008 = Fay/Gustav/Hanna/Ike)
- `aa.cerf_supplement (application_code, not_tc, valid_month_start, valid_year_start, valid_month_end, valid_year_end, confidence, notes, updated_at)` — `valid_*` hold the drought valid period (start/end month+year — it can span a year boundary); `confidence` is the Claude matcher's stated confidence for auto-applied picks (NULL = set by a human)

Both keyed on `ApplicationCode` (**not** `ApplicationID` — reused across unrelated
allocations in the feed). One join gives allocation × storm × track:

```sql
SELECT a.country_name, a.year, i.name, i.season
FROM aa.cerf_allocation_storm s
JOIN aa.cerf_allocation  a USING (application_code)
JOIN storms.ibtracs_storms i USING (sid);
```

`src/storage.py` reads/writes these tables (same DataFrame API as before). The
old blob parquet (`global/cerf/cerf_supplemental_data.parquet`) is retired;
`scripts/migrate_blob_to_db.py` did the one-off migration.

### The `aa.cerf_allocation` mirror

The storm tables join to `aa.cerf_allocation` — a **pure mirror** of the OneGMS feed,
and this repo is its **sole writer**: `scripts/refresh_mirror.py` keeps it current with
a daily **upsert** (feed columns + the deterministic `aa_keyword`), keyed on
`ApplicationCode`. Everything AA-interpretive lives in separate KB-owned tables
(`aa.actual_activation` + the curated `aa.activation_allocation` crosswalk, maintained
by ds-knowledge-base's `aa-links` confirm flow) — this script never touches those.

### The CBPF mirrors (schema `aa` + schema `cbpf`)

This repo is the home of **all** OneGMS mirrors, CBPF included. Two layers:

- **Normalized, AA-facing** (schema `aa`, refreshed by `refresh-mirror.yml`):
  `aa.cbpf_allocation` + `aa.cbpf_fund` (`scripts/refresh_cbpf.py`, also the
  fund-agnostic `aa.v_allocation` view) and `aa.cbpf_project` +
  `_cluster` + `_subip` (`scripts/refresh_cbpf_projects.py`).
- **Complete raw mirror of the public CBPF API** (schema `cbpf`, 74 tables + 3 views,
  `scripts/refresh_cbpf_full.py`, own daily workflow
  [refresh-cbpf-full.yml](.github/workflows/refresh-cbpf-full.yml)): one table per
  public surface of `cbpfapi.unocha.org` — the 28 vo3 and 9 surviving vo1 OData
  entity sets, the 33 public `GlobalGenericDataExtract` stored queries — plus the
  public Beneficiary Data Tool (deduplicated people: per fund, allocation — global and US scenario — and template, with and without admin locations; group cuts are views). Columns keep the API's names
  (snake_cased) and are typed from `$metadata` or by inference; each table is
  full-replaced daily and carries `fetched_at`; `cbpf.mirror_run` logs every load
  (rows, requests, seconds, key uniqueness) — the hook for monthly snapshots later.
  The registry (`src/cbpf_registry.py`) is the single place that says what is
  mirrored, how it is fetched, and what joins to what; its tail lists what was
  probed and left out (secured stored queries, superseded versions, dead vo1 sets).

The **ERD** of the whole CBPF mirror, with live row counts and column lists, is
published at **https://ocha-dap.github.io/ds-cerf-supplement/mirror/**
(`site/mirror/index.html`, fed by `scripts/export_cbpf_erd.py` on every deploy).

### Deriving what the AIT reporting pipeline reads

The mirror was scoped against the *OneGMS Public API Reference* written for
`ds-ait-reporting`. Every public endpoint that pipeline consumes has a home here, so
its exports can be rebuilt from the DB instead of the API. Rule: a `cbpf.*` **table** is a
verbatim API response; anything derivable is a **view** (`cbpf.v_*`), never a table.

| Reference endpoint | Where it is now |
| --- | --- |
| `PF_PROJ_SUMMARY_V4` / `PF_PROJ_DETAIL` / `PF_ORG_SUMMARY` / `PF_GLB_STATUS` / `PF_GLB_INDIC` | `cbpf.pf_proj_summary_v4`, `cbpf.pf_proj_detail`, `cbpf.pf_org_summary`, `cbpf.pf_glb_status` (all InstanceTypeIds, column `instance_type_id`), `cbpf.pf_glb_indic` (all years the API serves, not just the current one) |
| `indicator_reference.csv` / `cluster_reference.csv` (static copies of `GLB_INDIC_MST`, `CLUSTER_LIST`) | live: `cbpf.glb_indic_mst`, `cbpf.cluster_list`, `cbpf.comm_clst_mst` |
| `AllocationTypes`, `MstPooledFund`, `Poolfund`, `PipelineProjectSummary`, `AllocationFlowByOrgType`, `LastModified` | same names, snake_cased, in `cbpf` |
| vo1 `ProjectSummary` (`ShowFullProjectInfo=1`), `Cluster`, `NarrativeReportBeneficiary` | `cbpf.project_summary_v1`, `cbpf.cluster_v1`, `cbpf.narrative_report_beneficiary_v1` |
| BDT `/templates/` | `cbpf.bdt_template` (`group_names`, `allocation_type_ids` as JSON text) |
| BDT `/beneficiary/?group_name=<group>` (US tranche groups), ± `isByLocation` | **views** `cbpf.v_bdt_reach_by_group`, `cbpf.v_bdt_reach_by_group_location` — the group route only enumerates the group's templates (same rows and figures as `template_name=`, no group-total row; verified 2026-09-21), so the group cut is `bdt_reach_by_template[_location]` joined to `bdt_template.group_names`. Combined ≠ T1 + T2 still holds: they are different templates |
| BDT `/beneficiary/?year=&only_allocation=1&allocation_category=US\|ALL` | `cbpf.bdt_reach_by_allocation_us[_location]` (US scenario — matches the tranche templates) and `cbpf.bdt_reach_by_allocation[_location]` (global scenario) |
| BDT `/beneficiary/?template_name=`, `/beneficiaryByDisabilities/` | `cbpf.bdt_reach_by_template[_location]`, `cbpf.bdt_disability_by_template`, `cbpf.bdt_disability_by_fund`, view `cbpf.v_bdt_disability_by_group` |
| NSFT (US Award) filter | not a table: `where chf_project_code like '%NSFT%'` on any project table |
| `NARR_RPT_SUMMARY`, `MONITORING_SUMMARY`, `SUB_IP_OneGMS`, `REVISION_OneGMS`, `WORKPLAN_OneGMS`, `PF_ORG_DETAIL` | **not mirrored** — secured (`cbpfapib` credentials); the public sub-grant signal is `cbpf.allocation_flow_by_org_type` + the sub-IP cells in `cbpf.project_summary` / `aa.cbpf_project_subip` |
| History (snapshots, change detection) | not built yet — `cbpf.mirror_run` + `fetched_at` are the hooks |

## Local setup

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e .          # ocha-stratus from the local sibling path
cp .env.example .env         # fill in env vars
python scripts/export_site_data.py     # build site/data.json
python -m http.server -d site 8000     # preview the page at localhost:8000
```

Env vars (see `.env.example`): `DSCI_AZ_DB_DEV_HOST` / `_UID` / `_PW` (read),
`DSCI_AZ_DB_DEV_UID_WRITE` / `_PW_WRITE` (to save annotations), and
`PGSSLMODE=require`. The DB connection uses `ocha_stratus.get_engine()` — standard
OCHA stratus setup applies.

## The static site

`site/index.html` is the landing page (cards per product; `site/assets/` holds the
HDX v2 stylesheet + particle hero copied from `ds-seas5-skill`). Every product page
starts with the team's back-to-home button and lives under its own path:
`site/review/`, `site/mirror/`. `site/review/index.html` is a dependency-free page
that fetches `site/review/data.json` and
renders two searchable, sortable tabs: **Storms** (allocations × matched
IBTrACS storm(s), matched/needs-storm filter) and **Droughts** (allocations ×
valid rainfall-deficit period + confidence + notes, dated/needs-period filter),
each with CSV download. `review/data.json` is generated by
`scripts/export_site_data.py` and **not committed** (rebuilt on every deploy).
Pages source must be set to **GitHub Actions**.

## The daily pipeline (GitHub Actions)

Four chained workflows. `refresh-mirror` is the only one on a schedule; the rest
fire in order via `workflow_run` (so they run against a freshly-mirrored feed —
and the matchers never write `aa.cerf_supplement` concurrently), and each is
also runnable on demand from the Actions tab.

```
Refresh OneGMS mirror  (cron 05:30 UTC)
        └─▶ Match storms  (deterministic → Claude)
                └─▶ Match droughts  (Claude)
                        └─▶ Deploy site
```

### 1. Refresh OneGMS mirror — `refresh-mirror.yml`

Runs `scripts/refresh_mirror.py`: upserts the full CERF feed into `aa.cerf_allocation`
(see [the mirror](#the-aacerf_allocation-mirror) above). Preview locally with
`python scripts/refresh_mirror.py --dry-run`.

### 2. Match storms — `match-storms.yml`

Runs after the mirror. Two jobs:

- **`deterministic`** (`scripts/check_storm_sids.py --write`): finds every storm allocation with no SID yet, parses the storm name(s) from the title, and **backfills** the SID(s) when every named storm resolves to exactly one IBTrACS storm within ±1 year (handles multi-storm titles like "TC Batsirai & Emnati"). For anything it can't resolve it **opens a GitHub issue** (label `cerf-sid`, assigned `@t-downing`) with candidate storms and research links, **flags `not_tc`** allocations that will never be in IBTrACS, and **auto-closes** an issue once its allocation is resolved.
- **`claude`**: `prepare_claude_input.py` dumps the still-unresolved allocations (+ their candidate IBTrACS storms, + any human replies on open issues) → **Claude Code** (tools limited to Read/Write/WebSearch/WebFetch — **no** DB access) researches and writes `claude_work/matches.json` → `apply_claude_matches.py` validates each match (SID exists in IBTrACS, season within ±1 year) and writes only **confidence ≥ 0.8** results to the DB; lower-confidence suggestions are posted as issue comments to review.

### 3. Match droughts — `match-drought.yml`

Runs after the storm matcher (matchers chain rather than fan out — both write
`aa.cerf_supplement` via a transactional full-replace, so they must not run
concurrently). No deterministic stage (a failed rainy season can't be parsed
out of a title): `prepare_drought_input.py` dumps every undated RR drought
allocation with its OneGMS narratives (+ any human replies on open
`cerf-drought` issues, authoritative) → **Claude Code** identifies the
**rainfall-deficit months** (e.g. the failed *deyr* Oct–Dec 2016 behind a
March-2017 allocation) with a confidence → `apply_drought_matches.py` validates
(months 1–12, end ≥ start, span ≤ 24 months, within 2 years of the allocation)
and writes only **confidence ≥ 0.8** periods (confidence + reasoning stored on
the row); the rest get a `cerf-drought` issue with Claude's suggestion for a
human to confirm.

Adding yet another matcher = a new workflow chained off the last matcher via
`workflow_run` (serialize the `aa.cerf_supplement` writers), with deploy-site's
trigger moved to it.

### 4. Deploy site — `deploy-site.yml`

Runs after the last matcher (plus on push to `site/**` and a daily 08:00 UTC
backstop). Rebuilds `site/data.json` from the DB and deploys to GitHub Pages.

### Required repo secrets

(Settings → Secrets and variables → Actions.) DB: `DSCI_AZ_DB_DEV_HOST`,
`DSCI_AZ_DB_DEV_UID`, `DSCI_AZ_DB_DEV_PW`, `DSCI_AZ_DB_DEV_UID_WRITE`,
`DSCI_AZ_DB_DEV_PW_WRITE`. Claude matcher: **`CLAUDE_CODE_OAUTH_TOKEN`** (generate
with `claude setup-token`). `GITHUB_TOKEN` is provided automatically.

## Project structure

```
site/
  index.html            # static GitHub Pages page (vanilla JS, no build)
  data.json             # generated by export_site_data.py (git-ignored)
src/
  cerf_api.py           # Fetch + parse OneGMS XML → DataFrame
  db.py                 # Load storms from storms.ibtracs_storms
  storage.py            # Read/write aa.cerf_supplement + aa.cerf_allocation_storm (dev DB)
prompts/
  match_storms.md       # Instructions for the daily Claude storm matcher
  match_droughts.md     # Instructions for the daily Claude drought matcher
scripts/
  refresh_mirror.py       # Daily: upsert the OneGMS feed into aa.cerf_allocation
  export_site_data.py     # Build site/data.json (storm matches + drought periods)
  check_storm_sids.py     # Daily: backfill title-resolvable SIDs, manage issues
  prepare_claude_input.py # Daily: dump unresolved storm allocations + candidates
  apply_claude_matches.py # Daily: validate + apply Claude's high-confidence matches
  prepare_drought_input.py  # Daily: dump undated drought allocations + narratives
  apply_drought_matches.py  # Daily: validate + apply drought periods, manage issues
  raise_review_issues.py  # One-off: open review issues for low-confidence matches
  migrate_blob_to_db.py   # One-off: moved the supplement from blob → DB
  seed_from_existing.py   # One-off: rebuild SIDs from the tropicalcyclones CSV
  fill_guessed_sids.py    # One-off: high-confidence SID guesses from titles
  finalize_storms.py      # One-off: flag not_tc + summary-based matches
```
