"""The table registry for the raw CBPF mirror (schema ``cbpf``) — one TableSpec per
public API surface. Everything below was probed live on 2026-09-18; the "not
mirrored" list at the bottom records what was probed and left out, and why.

Groups (ERD lanes): fund · allocation · project · partner · people · reporting ·
finance · master · bdt.

Join edges are **by convention** — the API declares no foreign keys. The one
identifier every feed agrees on is the project code (``ChfProjectCode`` /
``CHFProjectCode`` / ``PrjCode`` — same value, three spellings); allocations are
``(PooledFundId, AllocationTypeId)`` (the id alone collides across funds).
"""
from __future__ import annotations

from datetime import date

from src.cbpf_mirror import FANOUT_FUND, FANOUT_YEAR, TableSpec

YEARS = list(range(2014, date.today().year + 1))

# ---------------------------------------------------------------- vo3 entity sets
# Typed by $metadata. "fanout=fund" = the unfiltered call 404s/times out, so one
# call per distinct PFAbbrv (34); otherwise one call returns the whole portfolio.
ENTITY_SETS = [
    TableSpec("mst_pooled_fund", "es", "MstPooledFund", key=("PFId",), group="fund",
              desc="Fund registry (46: country funds + regional RhPF envelopes and their children via ParentPFId). PFAbbrv is the request code, shared by regional children."),
    TableSpec("poolfund", "es", "Poolfund", key=("Id",), group="fund",
              joins=[("Id", "mst_pooled_fund.PFId")],
              desc="Fund lookup: id, name, code, lat/long, ISO2, parent."),
    TableSpec("last_modified", "es", "LastModified", group="master",
              desc="One row: when OneGMS's reporting DB last refreshed."),
    TableSpec("allocation_types", "es", "AllocationTypes", key=("PooledFundId", "AllocationTypeId"), group="allocation",
              joins=[("PooledFundId", "mst_pooled_fund.PFId")],
              desc="Every allocation envelope (Standard/Reserve) with title, summary, year, planned + approved budgets and project counts. Includes CERF rows (FundTypeId=2) — the raw feed is kept whole here; aa.cbpf_allocation is the CBPF-only normalized view."),
    TableSpec("extended_allocation_details", "es", "ExtendedAllocationDetails", group="allocation",
              desc="Allocation-level targets by population group (host/refugee/returnee/IDP/other × M/W/B/G) + theme, location, date. No allocation id — joins by title/fund/date only."),
    TableSpec("allocation_flow_by_org_type", "es", "AllocationFlowByOrgType", group="finance",
              joins=[("fund", "mst_pooled_fund.PFId")],
              desc="Sankey cells fund→org-type (targetType 1) and org-type→org-type pass-through (targetType 2) per year: the only public sub-granting history."),
    TableSpec("allocation_count_by_year_and_fund", "es", "AllocationCountByYearAndFund", key=("AllocationYear", "PooledFundName"), group="allocation",
              desc="Per fund-year: approved / pipeline project, partner counts and budgets."),
    TableSpec("allocation_budget_totals_by_year_and_fund", "es", "AllocationBudgetTotalsByYearAndFund", group="allocation",
              desc="Per fund-year × org type: approved and pipeline budgets split Standard/Reserve."),
    TableSpec("project_summary", "es", "ProjectSummary", fanout=FANOUT_FUND, group="project",
              joins=[("ChfProjectCode", "pf_proj_summary_v4.ChfProjectCode"), ("PooledFundId", "mst_pooled_fund.PFId"),
                     ("AllocationTypeId", "allocation_types.AllocationTypeId")],
              desc="Approved projects EXPLODED per cluster × admin location (64 cols: org, budget, dates, status, M/W/B/G, cluster %, location budgets, ##-delimited sub-IPs). aa.cbpf_project* is the normalized form."),
    TableSpec("project_summary_v2", "es", "ProjectSummaryV2", key=("PrjCode",), group="project",
              joins=[("PrjCode", "pf_proj_summary_v4.ChfProjectCode"), ("PFId", "mst_pooled_fund.PFId")],
              desc="One row per project (16.3k), abbreviated column names; carries direct/support cost split (BgdDC/BdgSC), OPS code, emergency, gender marker."),
    TableSpec("project_summary_agg_v2", "es", "ProjectSummaryAggV2", group="project",
              joins=[("PrjCode", "project_summary_v2.PrjCode")],
              desc="Project × admin-location rows (80k) with ##-aggregated cluster/beneficiary/budget cells per admin level 1–6."),
    TableSpec("project_summary_beneficiary_detail", "es", "ProjectSummaryBeneficiaryDetail", key=("ChfProjectCode",), group="people",
              joins=[("ChfProjectCode", "pf_proj_summary_v4.ChfProjectCode"), ("OrganizationId", "pf_org_summary.OrganizationId")],
              desc="Per project: planned vs actual M/W/B/G/total (cumulative, not deduplicated)."),
    TableSpec("pool_fund_beneficiary_summary", "es", "PoolFundBeneficiarySummary", group="people",
              joins=[("PooledFundId", "mst_pooled_fund.PFId")],
              desc="Fund × year × cluster × allocation source × org type: planned/actual people, cluster budget, project/partner/report counts."),
    TableSpec("pipeline_project_summary", "es", "PipelineProjectSummary", key=("ChfProjectCode",), group="project",
              joins=[("PooledFundId", "mst_pooled_fund.PFId")],
              desc="Projects still in approval (invisible to the approved feeds); allocation by title only, sub-IP cells ##-delimited."),
    TableSpec("narrative_report_logical_framework", "es", "NarrativeReportLogicalFramework", fanout=FANOUT_FUND, group="reporting",
              joins=[("ChfProjectCode", "pf_proj_summary_v4.ChfProjectCode")],
              desc="Per narrative report × cluster × output × indicator: planned vs actual M/W/B/G."),
    TableSpec("location_master", "es", "LocationMaster", fanout=FANOUT_FUND, group="master",
              joins=[("PooledFundId", "mst_pooled_fund.PFId")],
              desc="Each fund's admin-location tree (levels 1–5 with p-codes and coordinates) per allocation year."),
    TableSpec("mst_pf_admin_location_type", "es", "MstPFAdminLocationType", key=("PFId", "AdmLocTypeId"), group="master",
              joins=[("PFId", "mst_pooled_fund.PFId")],
              desc="Admin-level naming per fund (e.g. State / Locality)."),
    TableSpec("hfu_management_cost", "es", "HFUManagementCost", key=("Id",), group="finance",
              joins=[("PooledFundId", "mst_pooled_fund.PFId")],
              desc="Humanitarian Financing Unit management-cost budgets and expenditure per fund-year (feed repeats rows; Id is not unique after exact-dup removal)."),
    TableSpec("contribution_total", "es", "ContributionTotal", group="finance",
              desc="Donor contributions per fund per fiscal year: paid + pledged, USD and local currency."),
    TableSpec("donor_master", "es", "DonorMaster", group="master",
              desc="Donor registry with ISO codes, currency, GNP/GDP/population footnotes. DonorID is NOT unique (e.g. 132 = three names) — no key declared."),
    TableSpec("cbpf_summary", "es", "CBPFSummary", params={"year": YEARS[-1]}, group="finance",
              desc="One row of all-time global headline figures (donors, funds, contributions, allocations, projects, partners-by-type as JSON). Needs a ?year= to answer at all, but ignores its value."),
    TableSpec("project_gam_summary", "es", "ProjectGAMSummary", group="project",
              joins=[("PooledFundId", "mst_pooled_fund.PFId")],
              desc="Fund × year × gender-and-age-marker code: project count, beneficiaries, budget."),
    TableSpec("gender_marker", "es", "GenderMarker", group="master", desc="GAM code list."),
    TableSpec("mst_clusters", "es", "MstClusters", key=("ClustId",), group="master", desc="Global cluster list (17)."),
    TableSpec("mst_org_type", "es", "MstOrgType", key=("OrgTypeId",), group="master", desc="Organisation types (UN / INGO / NNGO / RC)."),
    TableSpec("mst_allocation_source", "es", "MstAllocationSource", key=("AllSrcId",), group="master", desc="Allocation sources (Standard / Reserve)."),
    TableSpec("sub_ip_type", "es", "SubIPType", key=("Id",), group="master", desc="Sub-implementing-partner type codes."),
]

# ------------------------------------------------------------ public stored queries
# GlobalGenericDataExtract?SPCode=… — fetched as CSV: the JSON form silently drops
# columns (PF_PROJ_SUMMARY_V4: 30 in JSON vs 68 in CSV). Blank AllocationYear(s) =
# all years the query covers. Whole-portfolio trick (verified 2026-09-18): send
# PoolfundCodeAbbrv= BLANK (present, empty — omitting it is an HTTP 500) plus
# ShowAllPooledFunds=1 and most queries return every fund in one call (V4: 16k rows,
# 18 s). ShowAllPooledFunds=1 with a fund set does NOT widen. ALLOCATION_TOTAL_V2/V3
# return nothing unless FundingType is 1 or 2 (both answer; blank = empty) → fanned
# out over FundingType.
_ALL = {"PoolfundCodeAbbrv": "", "ShowAllPooledFunds": 1}
_ALLYEARS = _ALL | {"AllocationYears": None, "FundTypeId": 1}
STORED_QUERIES = [
    TableSpec("pf_proj_summary_v4", "sp", "PF_PROJ_SUMMARY_V4", params=_ALLYEARS,
              key=("ChfProjectCode",), group="project",
              joins=[("PooledFundId", "mst_pooled_fund.PFId"), ("AllocationtypeId", "allocation_types.AllocationTypeId"),
                     ("OrgId", "pf_org_summary.OrganizationId")],
              desc="THE project record (68 cols): every stage incl. in-approval; budget, dates, status codes, targeted/reached M/W/B/G, disability, cluster-split people, GBV/GEQ/protection marker budgets, CVA people, risk ratings. PrjApprDate non-blank = approved. No title (see pf_proj_detail). 121 legacy 2014–15 codes appear twice with differing values (upstream), so the key is declared, not enforced."),
    TableSpec("pf_proj_detail", "sp", "PF_PROJ_DETAIL", params=_ALLYEARS,
              key=("ChfProjectCode",), group="project",
              joins=[("ChfProjectCode", "pf_proj_summary_v4.ChfProjectCode")],
              desc="Project title, external code, planned dates, submission/approval dates, marker ids, people columns."),
    TableSpec("pf_org_summary", "sp", "PF_ORG_SUMMARY", params={"FundTypeId": 1},
              key=("PooledFundId", "OrganizationId"), group="partner",
              joins=[("PooledFundId", "mst_pooled_fund.PFId"), ("GlobalOrgId", "pf_global_org.ParentOrganizationId")],
              desc="Organisation master per fund: name, acronym, type, due-diligence status, eligibility, first allocation, global org, localization marker, WLO/RLO/OPD/YLO flags."),
    TableSpec("pf_global_org", "sp", "PF_GLOBAL_ORG", group="partner",
              desc="Global (cross-fund) organisation identities (ParentOrganizationId ~unique; one id carries a blank + a named row)."),
    TableSpec("pf_glb_status", "sp", "PF_GLB_STATUS",
              params={"PoolfundCodeAbbrv": "SUD15", "FundTypeId": 1}, param_fanout=("InstanceTypeId", [1, 2, 3, 4, 5, 6, 7]),
              key=("PooledFundId", "InstanceTypeId", "GlobalInstanceStatusId", "AllocSrc"), group="master",
              desc="Status-code → label per fund and instance type (project, report, revision…), in the fund's language. Whole portfolio regardless of the (required) fund parameter."),
    TableSpec("pf_glb_indic", "sp", "PF_GLB_INDIC",
              params=_ALL | {"AllocationYears": None, "IndicatorTypeId": None, "FundTypeId": 1},
              group="reporting",
              joins=[("CHFProjectCode", "pf_proj_summary_v4.ChfProjectCode"), ("GlbIndicId", "glb_indic_mst.Id"), ("CommClstrId", "comm_clst_mst.Id")],
              desc="Project × global indicator × cluster: targeted and achieved M/W/B/G/total (OneGMS-era projects, 2023+)."),
    TableSpec("glb_indic_mst", "sp", "GLB_INDIC_MST", params={"GlobalIndicatorType": None}, key=("Id",), group="master",
              joins=[("CommClstrId", "comm_clst_mst.Id")],
              desc="Global cluster indicator master (675): code, unit (Percentage = average, else sum), core flag, cluster."),
    TableSpec("comm_clst_mst", "sp", "COMM_CLST_MST", key=("Id",), group="master", desc="Common (global) cluster master."),
    TableSpec("cluster_list", "sp", "CLUSTER_LIST", key=("PooledFundId", "ClusterId"), group="master",
              joins=[("PooledFundId", "mst_pooled_fund.PFId"), ("CommonClusterId", "comm_clst_mst.Id")],
              desc="Each fund's own cluster list mapped to global and common clusters."),
    TableSpec("pf_rpt_clst_benef", "sp", "PF_RPT_CLST_BENEF", params=_ALLYEARS, group="reporting",
              joins=[("ChfProjectCode", "pf_proj_summary_v4.ChfProjectCode"), ("AllocationTypeId", "allocation_types.AllocationTypeId")],
              desc="Narrative-report cluster beneficiaries per project × cluster: target vs actual M/W/B/G, cluster % and budget, report status/date."),
    TableSpec("apidat_cva", "sp", "APIDAT_CVA", params={"AllocationYear": None, "FundTypeId": 1}, group="reporting",
              joins=[("ChfProjectCode", "pf_proj_summary_v4.ChfProjectCode"), ("CVATypeId", "mst_cva_type.Id")],
              desc="Cash-and-voucher assistance per project × CVA type × cluster: people targeted/reached, transfer amounts (2023+)."),
    TableSpec("mst_cva_type", "sp", "MstCVAType", key=("Id",), group="master", desc="CVA type master."),
    TableSpec("project_emergency", "sp", "PROJECT_EMERGENCY_OneGMS", params={"PoolfundCodeAbbrv": "", "AllocationYear": None, "FundTypeId": 1},
              group="project",
              joins=[("CHFProjectCode", "pf_proj_summary_v4.ChfProjectCode"), ("EmergencyTypeId", "emerg_type_mst.EmergencyTypeId")],
              desc="Project × emergency type with % split (OneGMS-era projects)."),
    TableSpec("emerg_type_mst", "sp", "EMERG_TYPE_MST", key=("EmergencyTypeId",), group="master",
              joins=[("EmergencyGroupId", "emerg_grp_mst.EmergencyGroupId")], desc="Emergency types → categories → groups."),
    TableSpec("emerg_grp_mst", "sp", "EMERG_GRP_MST", key=("EmergencyGroupId",), group="master", desc="Emergency groups."),
    TableSpec("benef_type_mst", "sp", "BENEF_TYPE_MST", key=("Id",), group="master", desc="Affected-people / beneficiary types."),
    TableSpec("pf_marker_gbv", "sp", "PF_MARKER_GBV", params={"FundTypeId": None}, key=("Id",), group="master", desc="GBV marker codes (FundTypeId 1 = CBPF, 2 = CERF)."),
    TableSpec("pf_marker_geq", "sp", "PF_MARKER_GEQ", params={"FundTypeId": None}, key=("Id",), group="master", desc="Gender-equality marker codes (both fund types)."),
    TableSpec("pf_marker_disabl", "sp", "PF_MARKER_DISABL", params={"FundTypeId": None}, key=("Id",), group="master", desc="Disability marker codes (both fund types)."),
    TableSpec("pf_marker_cva", "sp", "PF_MARKER_CVA", params={"FundTypeId": None}, key=("Id",), group="master", desc="CVA marker codes (both fund types)."),
    TableSpec("all_poolfund", "sp", "ALL_POOLFUND", params=_ALL, key=("PFId",), group="fund",
              joins=[("PFId", "mst_pooled_fund.PFId")],
              desc="Fund master with FundStatus (Active/Closed) — the one field MstPooledFund lacks. 309 rows: every fund OneGMS knows (CERF-side and closed ones included), not just the 46 public CBPF ones."),
    TableSpec("allocation_v2", "sp", "ALLOCATION_V2",
              params=_ALL | {"AllocationYear": None, "FundTypeId": 1, "ShowNSFT": None},
              key=("PooledFundId", "AllocationTypeId"), group="allocation",
              joins=[("AllocationTypeId", "allocation_types.AllocationTypeId")],
              desc="AllocationTypes + LocationTemplate, CBPF only, per fund."),
    TableSpec("allocation_total_v2", "sp", "ALLOCATION_TOTAL_V2",
              params=_ALL | {"AllocationYearFrom": None, "AllocationYearTo": None, "ShowNSFT": None},
              param_fanout=("FundingType", [1, 2]),
              group="allocation", desc="Approved and pipeline budget totals per fund-year × org type × FundingType (1 and 2 — the column says which), Standard/Reserve split, 2010+."),
    TableSpec("allocation_total_v3", "sp", "ALLOCATION_TOTAL_V3",
              params=_ALL | {"AllocationYearFrom": None, "AllocationYearTo": None},
              param_fanout=("FundingType", [1, 2]),
              group="allocation", joins=[("CHFProjectCode", "pf_proj_summary_v4.ChfProjectCode")],
              desc="Same totals at project grain (adds CHFProjectCode), FundingType 1 and 2."),
    TableSpec("allocation_flow_nsft", "sp", "ALLOCATION_FLOW_NSFT",
              params=_ALL | {"AllocationYear": None}, group="finance",
              desc="Allocation flow by org type restricted to the 2026 US (NSFT) allocations, with process/project status."),
    TableSpec("proj_loc_map", "sp", "PROJ_LOC_MAP",
              params={"PoolfundCodeAbbrv": "", "GlobalOrganizationId": None, "PoolfundIds": None, "AllocationYear": None}, group="project",
              joins=[("PrjCode", "pf_proj_summary_v4.ChfProjectCode"), ("OrganizationId", "pf_org_summary.OrganizationId")],
              desc="Project × admin-1 location map rows (all years): cluster/budget aggregates, coordinates, cycle status, org + parent org."),
    TableSpec("cbpf_global_proj_summary_agg_v4", "sp", "CBPF_Global_PROJ_SUMMARY_Agg_V4",
              params=_ALL | {"AdminLocationLevel": None, "AllocationYear": None}, group="project",
              joins=[("PrjCode", "pf_proj_summary_v4.ChfProjectCode")],
              desc="Data-warehouse project × location aggregate (levels 1–6, ##-cells, p-codes, targeted + reached per cluster) for OneGMS-era projects."),
    TableSpec("ar_contributions_by_donor", "sp", "AR_QUERY_2", params={"FiscalYear": None}, group="finance",
              joins=[("PooledFundId", "mst_pooled_fund.PFId")],
              desc="Annual-report query 2: contributions per fund × fiscal year × donor with % share (the only AR_QUERY that is public)."),
]

# ------------------------------------------------------------------ vo1 entity sets
# The older service (50 sets in $metadata, still live). Only 9 answer at all — the
# rest are HTTP 404/501 for every parameter form — and only those with no vo3
# equivalent are mirrored. Typed by the vo1 $metadata.
VO1_SETS = [
    TableSpec("project_summary_v1", "es", "ProjectSummary", version="vo1", fanout=FANOUT_FUND,
              params={"ShowFullProjectInfo": 1}, key=("ChfProjectCode",), group="project",
              joins=[("ChfProjectCode", "pf_proj_summary_v4.ChfProjectCode"), ("PooledFundId", "mst_pooled_fund.PFId"),
                     ("AllocationTypeId", "allocation_types.AllocationTypeId")],
              desc="One row per approved project, vo1 shape (52 cols): org name/type ON the row, allocation year, direct/support cost split, planned AND actual dates, partner code, and — with ShowFullProjectInfo=1 — the proposal narrative fields (summary, context, justification, activities, M&E, cross-cutting…). Nothing else public has these."),
    TableSpec("cluster_v1", "es", "Cluster", version="vo1", group="project",
              joins=[("ChfProjectCode", "pf_proj_summary_v4.ChfProjectCode"), ("PooledFundId", "mst_pooled_fund.PFId")],
              desc="Project × cluster × sub-cluster with % and cluster budget (24.7k rows) — the cluster budget split the vo3 feeds only carry ##-aggregated."),
    TableSpec("pipeline_project_summary_v1", "es", "PipelineProjectSummary", version="vo1", key=("ChfProjectCode",), group="project",
              joins=[("PooledFundId", "mst_pooled_fund.PFId")],
              desc="In-approval projects, vo1 shape (699 vs vo3's 531 — different snapshot rules); direct/support costs, planned dates."),
    TableSpec("pipeline_project_cluster_v1", "es", "PipelineProjectCluster", version="vo1", group="project",
              joins=[("ChfProjectCode", "pipeline_project_summary_v1.ChfProjectCode")],
              desc="Cluster split of in-approval projects."),
    TableSpec("project_summary_with_location_v1", "es", "ProjectSummaryWithLocation", version="vo1", fanout=FANOUT_FUND, group="project",
              joins=[("ChfProjectCode", "pf_proj_summary_v4.ChfProjectCode")],
              desc="Project × admin-1 location: p-code, coordinates, % of project."),
    TableSpec("project_summary_with_location_and_cluster_v1", "es", "ProjectSummaryWithLocationAndCluster", version="vo1", fanout=FANOUT_FUND, group="project",
              joins=[("ChfProjectCode", "pf_proj_summary_v4.ChfProjectCode")],
              desc="Project × cluster × admin-1 location (cluster % and location %)."),
    TableSpec("narrative_report_beneficiary_v1", "es", "NarrativeReportBeneficiary", version="vo1", fanout=FANOUT_FUND, group="reporting",
              joins=[("ChfProjectCode", "pf_proj_summary_v4.ChfProjectCode")],
              desc="Per narrative report × beneficiary type: planned vs actual M/W/B/G/total."),
    TableSpec("logical_framework_indicator_v1", "es", "LogicalFrameworkIndicator", version="vo1", fanout=FANOUT_FUND, group="reporting",
              joins=[("CHFProjectCode", "pf_proj_summary_v4.ChfProjectCode")],
              desc="Project logframe indicators (outcome/output code, indicator text, unit, M/W/B/G targets) — ~17k rows for Sudan alone."),
    TableSpec("contribution_v1", "es", "Contribution", version="vo1", group="finance",
              joins=[("PooledFundId", "mst_pooled_fund.PFId")],
              desc="Contribution-level donor records (4.5k): pledge/paid dates and amounts, local currency, transfer flag — finer than vo3 ContributionTotal."),
]

# --------------------------------------------------------------------------- BDT
# Beneficiary Data Tool (deduplicated people) — src/bdt_api.py. BDT rows join to
# allocations (AllocationTypeId) and templates, never to projects. `year` here is
# the implementation year; empty years just return no rows.
_BDT_YEARS = list(range(2019, date.today().year + 1))
BDT = [
    TableSpec("bdt_template", "bdt", "templates", key=("TemplateName",), group="bdt",
              desc="Template registry (51): which allocations (AllocationTypeIds) and groups each deduplication template covers — the only bridge from allocation to a BDT figure."),
    TableSpec("bdt_reach_by_allocation", "bdt", "beneficiary", fanout=FANOUT_YEAR, year_param="year", years=_BDT_YEARS,
              params={"only_allocation": 1, "allocation_category": "ALL", "process_status": "all", "isByLocation": "false"},
              group="bdt", joins=[("AllocationTypeId", "allocation_types.AllocationTypeId"), ("PFId", "mst_pooled_fund.PFId")],
              desc="Deduplicated people targeted/reached per fund × allocation × process status (global scenario)."),
    TableSpec("bdt_reach_by_allocation_location", "bdt", "beneficiary", fanout=FANOUT_YEAR, year_param="year", years=_BDT_YEARS,
              params={"only_allocation": 1, "allocation_category": "ALL", "process_status": "all", "isByLocation": "true"},
              group="bdt", joins=[("AllocationTypeId", "allocation_types.AllocationTypeId"), ("PFId", "mst_pooled_fund.PFId")],
              desc="Same, per admin location (LocPath / AdminName; Pcode usually blank)."),
    TableSpec("bdt_reach_by_fund", "bdt", "beneficiary", fanout=FANOUT_YEAR, year_param="year", years=_BDT_YEARS,
              params={"process_status": "all", "isByLocation": "false"}, group="bdt",
              joins=[("PFId", "mst_pooled_fund.PFId")],
              desc="Fund-level deduplicated total per implementation year — a fund's headline reach."),
    TableSpec("bdt_disability_by_fund", "bdt", "beneficiaryByDisabilities", fanout=FANOUT_YEAR, year_param="year", years=_BDT_YEARS,
              params={"process_status": "all", "isByLocation": "false"}, group="bdt",
              joins=[("PFId", "mst_pooled_fund.PFId")],
              desc="Fund-level deduplicated people with disabilities per year (blank = not yet reported, not zero)."),
    TableSpec("bdt_reach_by_template", "bdt", "beneficiary",
              param_fanout=("template_name", lambda: __import__("src.bdt_api", fromlist=["template_names"]).template_names()),
              params={"process_status": "all", "isByLocation": "false"}, group="bdt",
              joins=[("TemplateName", "bdt_template.TemplateName")],
              desc="Each stored template's deduplicated figure (incl. the 2026 US tranche templates). Groups are separate queries, not sums — never add templates."),
    TableSpec("bdt_reach_by_template_location", "bdt", "beneficiary",
              param_fanout=("template_name", lambda: __import__("src.bdt_api", fromlist=["template_names"]).template_names()),
              params={"process_status": "all", "isByLocation": "true"}, group="bdt",
              joins=[("TemplateName", "bdt_template.TemplateName")],
              desc="Each template's deduplicated figure per admin location (LocPath / AdminName). Rows within one template are additive; across templates they are not."),
    TableSpec("bdt_reach_by_group", "bdt", "beneficiary",
              param_fanout=("group_name", lambda: __import__("src.bdt_api", fromlist=["group_names"]).group_names()),
              params={"process_status": "all", "isByLocation": "false"}, group="bdt",
              joins=[("PFId", "mst_pooled_fund.PFId")],
              desc="Group-level deduplication per fund (US_Tranche1_2026, US_Tranche2_2026, US_Tranche_2026 = both tranches deduplicated across each other). Combined ≠ T1 + T2. AllocationTypeId is null on group rows; the group name is on every row. No year parameter (it would empty the feed)."),
    TableSpec("bdt_reach_by_group_location", "bdt", "beneficiary",
              param_fanout=("group_name", lambda: __import__("src.bdt_api", fromlist=["group_names"]).group_names()),
              params={"process_status": "all", "isByLocation": "true"}, group="bdt",
              joins=[("PFId", "mst_pooled_fund.PFId")],
              desc="Group-level deduplicated figures per fund × admin location."),
    TableSpec("bdt_disability_by_group", "bdt", "beneficiaryByDisabilities",
              param_fanout=("group_name", lambda: __import__("src.bdt_api", fromlist=["group_names"]).group_names()),
              params={"process_status": "all", "isByLocation": "false"}, group="bdt",
              joins=[("PFId", "mst_pooled_fund.PFId")],
              desc="Group-level deduplicated people with disabilities per fund (blank = not yet reported)."),
    TableSpec("bdt_reach_by_allocation_us", "bdt", "beneficiary", fanout=FANOUT_YEAR, year_param="year", years=_BDT_YEARS,
              params={"only_allocation": 1, "allocation_category": "US", "process_status": "all", "isByLocation": "false"},
              group="bdt", joins=[("AllocationTypeId", "allocation_types.AllocationTypeId"), ("PFId", "mst_pooled_fund.PFId")],
              desc="Per-allocation figures under the US deduplication scenario (GT_US) — the one that matches the tranche templates; differs from the global scenario in bdt_reach_by_allocation for the same allocation."),
    TableSpec("bdt_reach_by_allocation_us_location", "bdt", "beneficiary", fanout=FANOUT_YEAR, year_param="year", years=_BDT_YEARS,
              params={"only_allocation": 1, "allocation_category": "US", "process_status": "all", "isByLocation": "true"},
              group="bdt", joins=[("AllocationTypeId", "allocation_types.AllocationTypeId"), ("PFId", "mst_pooled_fund.PFId")],
              desc="US-scenario per-allocation figures per admin location."),
]

ALL: list[TableSpec] = ENTITY_SETS + STORED_QUERIES + VO1_SETS + BDT

# ---------------------------------------------------------------- not mirrored
# Probed 2026-09-18 and deliberately left out:
#   PF_PROJ_SUMMARY / _V2 / _V3  — strict subsets of _V4 (same rows, fewer columns)
#   CBPF_Global_PROJ_SUMMARY_Agg_V3 — subset of _V4 (no p-codes)
#   MstAllocationSource (stored query) — identical to the entity set
#   HRPCBPFFundingSummary — 404 for every parameter form tried
#   94 secured SPCodes (AR_QUERY_1,3–45, BOA_*, *_OneGMS full dumps, NARR_RPT_SUMMARY,
#     MONITORING_SUMMARY, REVISION_OneGMS, SUB_IP_OneGMS, PF_ORG_DETAIL, APIDAT_CERF_*,
#     TAG_PRJ*, MPTFExtract …) — HTTP 200 {"Error": "This is secure procedure…"} on
#     the public host; they need the cbpfapib GlobalGenericDataExtractSecure creds.


def by_name(names: list[str]) -> list[TableSpec]:
    idx = {s.name: s for s in ALL}
    missing = [n for n in names if n not in idx]
    if missing:
        raise SystemExit(f"unknown table(s): {missing}. Known: {sorted(idx)}")
    return [idx[n] for n in names]
