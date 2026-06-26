import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from src.cerf_api import fetch_cerf_allocations
from src.db import load_storms
from src.storage import (
    decode_sids,
    encode_sids,
    load_supplemental,
    remove_annotation,
    save_supplemental,
    upsert_annotation,
)

load_dotenv()

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH_NUM = {m: i + 1 for i, m in enumerate(MONTHS)}
MONTH_NAME = {i + 1: m for i, m in enumerate(MONTHS)}
EDITABLE_COLS = ["Storm", "Start Month", "Start Year", "End Month", "End Year", "Notes"]

st.set_page_config(page_title="CERF Supplement", layout="wide")
st.title("CERF Allocation Supplement")

# --- Session state ---
for k, v in [("supp_df", None), ("needs_rebuild", True), ("prev_filters", None), ("editor_version", 0)]:
    if k not in st.session_state:
        st.session_state[k] = v

# --- Load data ---
with st.spinner("Loading…"):
    cerf_df = fetch_cerf_allocations()
    storms_df = load_storms()

if st.session_state.supp_df is None:
    st.session_state.supp_df = load_supplemental()
supp_df: pd.DataFrame = st.session_state.supp_df

# --- Storm lookup maps ---
storms_df = storms_df.dropna(subset=["name"])
storm_labels = [
    f"{r['name']} {int(r['season'])} ({r['sid']})"
    for _, r in storms_df.iterrows()
]
sid_to_label = dict(zip(storms_df["sid"], storm_labels))
label_to_sid = {v: k for k, v in sid_to_label.items()}


def label_for(sid: str) -> str:
    """Storm label, falling back to the raw SID for unnamed storms."""
    return sid_to_label.get(sid, sid)


# per-ApplicationCode list of SIDs (from current supplemental data)
sids_map: dict[str, list[str]] = {}
if not supp_df.empty:
    sids_map = {r["ApplicationCode"]: decode_sids(r["sids"]) for _, r in supp_df.iterrows()}

# --- Sidebar ---
with st.sidebar:
    if st.button("🔄 Refresh data"):
        fetch_cerf_allocations.clear()
        load_storms.clear()
        st.session_state.supp_df = None
        st.session_state.needs_rebuild = True
        st.session_state.editor_version += 1
        st.rerun()

    all_types = sorted(cerf_df["EmergencyTypeName"].dropna().unique())
    type_filter = st.selectbox("Emergency type", ["All"] + all_types)
    status_filter = st.selectbox("Show", ["All", "Needs annotation", "Annotated"])

    if not os.getenv("DSCI_AZ_BLOB_DEV_SAS"):
        st.warning("DSCI_AZ_BLOB_DEV_SAS not set — saves will fail.")

# --- Build display DataFrame ---
annotated_codes = set(supp_df["ApplicationCode"]) if not supp_df.empty else set()

filtered = cerf_df.copy()
if type_filter != "All":
    filtered = filtered[filtered["EmergencyTypeName"] == type_filter]
if status_filter == "Annotated":
    filtered = filtered[filtered["ApplicationCode"].isin(annotated_codes)]
elif status_filter == "Needs annotation":
    filtered = filtered[~filtered["ApplicationCode"].isin(annotated_codes)]

supp_cols = ["ApplicationCode", "valid_month_start", "valid_year_start", "valid_month_end", "valid_year_end", "notes"]
if not supp_df.empty:
    merged = filtered.merge(supp_df[supp_cols], on="ApplicationCode", how="left")
else:
    merged = filtered.assign(
        valid_month_start=None, valid_year_start=None,
        valid_month_end=None, valid_year_end=None, notes=None,
    )


def _fmt_amount(x) -> str:
    try:
        return f"${float(x):,.0f}" if pd.notna(x) else ""
    except (TypeError, ValueError):
        return ""


def _fmt_month(x):
    try:
        return MONTH_NAME.get(int(x)) if pd.notna(x) else None
    except (TypeError, ValueError):
        return None


# per-row storm lists, primary (first) label, and full joined list
row_sids = [sids_map.get(code, []) for code in merged["ApplicationCode"]]
primary_labels = [label_for(s[0]) if s else None for s in row_sids]
all_storm_labels = ["; ".join(label_for(s) for s in lst) for lst in row_sids]

# .values on every column to avoid pandas index-alignment producing all-None
alloc_dates = (
    pd.to_datetime(merged["CN_ERC_EndorsementDate"], errors="coerce")
    .dt.strftime("%Y-%m-%d")
    .fillna("")
    .values
)

edit_df = pd.DataFrame(
    {
        "Code": merged["ApplicationCode"].values,
        "Allocation Date": alloc_dates,
        "Country": merged["CountryName"].values,
        "Type": merged["EmergencyTypeName"].values,
        "Amount": [_fmt_amount(x) for x in merged["TotalAmountApproved"]],
        "Storm": primary_labels,
        "All storms": all_storm_labels,
        "Start Month": [_fmt_month(x) for x in merged["valid_month_start"]],
        "Start Year": merged["valid_year_start"].values,
        "End Month": [_fmt_month(x) for x in merged["valid_month_end"]],
        "End Year": merged["valid_year_end"].values,
        "Notes": merged["notes"].values,
    },
    index=merged["ApplicationCode"].values,
)
edit_df.index.name = "ApplicationCode"

# Storm dropdown options must include any primary labels already present
# (e.g. unnamed storms shown by raw SID) so the editor doesn't reject them.
extra_labels = sorted({lbl for lbl in primary_labels if lbl and lbl not in label_to_sid})
storm_options = [None] + storm_labels + extra_labels

# --- Rebuild baseline when filters or data change ---
current_filters = (type_filter, status_filter)
if st.session_state.prev_filters != current_filters or st.session_state.needs_rebuild:
    st.session_state.prev_filters = current_filters
    st.session_state.needs_rebuild = False
    st.session_state.baseline_df = edit_df.copy()
    st.session_state.editor_version += 1

baseline_df: pd.DataFrame = st.session_state.baseline_df

# --- Data editor ---
st.caption(f"{len(baseline_df)} allocations")

returned_df = st.data_editor(
    baseline_df,
    column_config={
        "Code": st.column_config.TextColumn(disabled=True, width="small"),
        "Allocation Date": st.column_config.TextColumn(disabled=True, width="small"),
        "Country": st.column_config.TextColumn(disabled=True, width="small"),
        "Type": st.column_config.TextColumn(disabled=True, width="medium"),
        "Amount": st.column_config.TextColumn(disabled=True, width="small"),
        "Storm": st.column_config.SelectboxColumn(
            options=storm_options,
            width="medium",
            help="Primary storm (searchable). For multiple storms, use the multi-storm editor below.",
        ),
        "All storms": st.column_config.TextColumn(
            disabled=True, width="medium",
            help="All assigned storms (read-only). Edit multiples in the multi-storm editor below.",
        ),
        "Start Month": st.column_config.SelectboxColumn(options=[None] + MONTHS, width="small"),
        "Start Year": st.column_config.NumberColumn(min_value=1990, max_value=2035, step=1, format="%d", width="small"),
        "End Month": st.column_config.SelectboxColumn(options=[None] + MONTHS, width="small"),
        "End Year": st.column_config.NumberColumn(min_value=1990, max_value=2035, step=1, format="%d", width="small"),
        "Notes": st.column_config.TextColumn(width="medium"),
    },
    hide_index=True,
    use_container_width=True,
    num_rows="fixed",
    key=f"editor_{st.session_state.editor_version}",
)


# --- Detect changes and save (inline edits) ---
def _str(val):
    s = str(val).strip()
    return s if s and s != "nan" else None


def _year(val):
    try:
        return int(val) if pd.notna(val) and _str(val) else None
    except (ValueError, TypeError):
        return None


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    return df[EDITABLE_COLS].fillna("").astype(str)


changed_mask = (_normalize(baseline_df) != _normalize(returned_df)).any(axis=1)
n_changes = int(changed_mask.sum())

if n_changes:
    if st.button(f"💾 Save {n_changes} change{'s' if n_changes != 1 else ''}", type="primary"):
        updated = supp_df.copy() if not supp_df.empty else pd.DataFrame(columns=["ApplicationCode", "sids", *supp_cols[1:], "updated_at"])

        for app_code in returned_df.index[changed_mask]:
            r = returned_df.loc[app_code]

            # Storm column edits only the primary; preserve any extra storms
            # unless the primary itself was changed.
            existing_sids = sids_map.get(app_code, [])
            new_primary_label = _str(r["Storm"])
            if new_primary_label != _str(baseline_df.loc[app_code, "Storm"]):
                if new_primary_label:
                    new_sid = label_to_sid.get(new_primary_label, new_primary_label)
                    new_sids = [new_sid] + [s for s in existing_sids if s != new_sid]
                else:
                    new_sids = []  # primary cleared -> drop all storms
            else:
                new_sids = existing_sids

            start_m = MONTH_NUM.get(_str(r["Start Month"]))
            start_y = _year(r["Start Year"])
            end_m = MONTH_NUM.get(_str(r["End Month"]))
            end_y = _year(r["End Year"])
            notes = _str(r["Notes"])

            if new_sids or any([start_m, start_y, end_m, end_y, notes]):
                updated = upsert_annotation(updated, app_code, {
                    "sids": encode_sids(new_sids),
                    "valid_month_start": start_m,
                    "valid_year_start": start_y,
                    "valid_month_end": end_m,
                    "valid_year_end": end_y,
                    "notes": notes,
                })
            else:
                updated = remove_annotation(updated, app_code)

        try:
            save_supplemental(updated)
            st.session_state.supp_df = updated
            st.session_state.needs_rebuild = True
            st.success(f"Saved {n_changes} change{'s' if n_changes != 1 else ''}.")
            st.rerun()
        except Exception as e:
            st.error(f"Save failed: {e}")

# --- Multi-storm editor ---
st.divider()
st.subheader("Multi-storm editor")
st.caption("Assign several storms to one allocation (e.g. a season that hit a country with multiple cyclones).")

app_codes = list(merged["ApplicationCode"])
country_by_code = dict(zip(merged["ApplicationCode"], merged["CountryName"]))
year_by_code = dict(zip(merged["ApplicationCode"], merged["Year"]))


def _alloc_label(code: str) -> str:
    n = len(sids_map.get(code, []))
    tag = f"  ·  {n} storm{'s' if n != 1 else ''}" if n else ""
    return f"{code} — {country_by_code.get(code)} {year_by_code.get(code)}{tag}"


if not app_codes:
    st.info("No allocations match the current filters.")
else:
    sel_code = st.selectbox("Allocation", app_codes, format_func=_alloc_label, key="ms_alloc")
    current = sids_map.get(sel_code, [])
    current_labels = [label_for(s) for s in current]
    ms_options = storm_labels + [lbl for lbl in current_labels if lbl not in label_to_sid]
    chosen = st.multiselect(
        "Storms",
        options=ms_options,
        default=current_labels,
        help="Search by storm name or SID. Add as many as apply.",
        key=f"ms_storms_{sel_code}",
    )
    if st.button("💾 Save storms"):
        new_sids = [label_to_sid.get(lbl, lbl) for lbl in chosen]
        updated = supp_df.copy() if not supp_df.empty else pd.DataFrame(columns=["ApplicationCode", "sids", *supp_cols[1:], "updated_at"])
        existing_row = updated[updated["ApplicationCode"] == sel_code]
        keep = existing_row.iloc[0].to_dict() if not existing_row.empty else {}
        if new_sids or any(keep.get(c) is not None and not pd.isna(keep.get(c)) for c in supp_cols[1:]):
            updated = upsert_annotation(updated, sel_code, {
                "sids": encode_sids(new_sids),
                "valid_month_start": keep.get("valid_month_start"),
                "valid_year_start": keep.get("valid_year_start"),
                "valid_month_end": keep.get("valid_month_end"),
                "valid_year_end": keep.get("valid_year_end"),
                "notes": keep.get("notes"),
            })
        else:
            updated = remove_annotation(updated, sel_code)
        try:
            save_supplemental(updated)
            st.session_state.supp_df = updated
            st.session_state.needs_rebuild = True
            st.success(f"Saved {len(new_sids)} storm(s) for {sel_code}.")
            st.rerun()
        except Exception as e:
            st.error(f"Save failed: {e}")
