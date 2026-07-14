"""
Supplemental CERF↔storm data, stored in the dev DB (schema `aa`), alongside the
KB's `aa.cerf_allocation` feed. Two normalized tables:

  aa.cerf_allocation_storm(application_code, sid)   -- one row per matched storm
  aa.cerf_supplement(application_code, not_tc, not_drought, valid_month_start,
                     valid_year_start, valid_month_end, valid_year_end, confidence,
                     notes, updated_at)

`valid_*` hold the meteorological drought period (rainfall-deficit months) for
drought allocations — start/end month+year, since a drought can span a year
boundary and often precedes the allocation by up to a year. `confidence` is the
Claude matcher's stated confidence for auto-applied picks (NULL = set by a human).

Keyed on ApplicationCode (ApplicationID is NOT unique in the CERF feed). Joinable
to aa.cerf_allocation and storms.ibtracs_storms.

The public API (load/save/upsert/remove + encode/decode/is_resolved) is unchanged:
callers work with a DataFrame whose `sids` column is a JSON list string, exactly
as before — only the backing store moved from blob parquet to the DB.
"""

import json
import os
from datetime import datetime, timezone

import pandas as pd
import ocha_stratus as stratus
from sqlalchemy import text

os.environ.setdefault("PGSSLMODE", "require")

SCHEMA = "aa"
_COLUMNS = [
    "ApplicationCode",
    "sids",
    "not_tc",
    "not_drought",
    "valid_month_start",
    "valid_year_start",
    "valid_month_end",
    "valid_year_end",
    "confidence",
    "notes",
    "updated_at",
]
_SUPP_COLS = ["not_tc", "not_drought", "valid_month_start", "valid_year_start",
              "valid_month_end", "valid_year_end", "confidence", "notes"]


def encode_sids(sids: list[str] | None) -> str | None:
    sids = [s for s in (sids or []) if s]
    return json.dumps(sids) if sids else None


def decode_sids(value) -> list[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    if isinstance(value, list):
        return [s for s in value if s]
    s = str(value).strip()
    if not s or s == "nan":
        return []
    try:
        parsed = json.loads(s)
        return [x for x in parsed if x] if isinstance(parsed, list) else [s]
    except (json.JSONDecodeError, TypeError):
        return [s]


def is_resolved(row) -> bool:
    return bool(decode_sids(row.get("sids"))) or bool(row.get("not_tc"))


def has_valid_period(row) -> bool:
    """True when the row carries a complete meteorological drought period."""
    return all(
        pd.notna(row.get(k)) and str(row.get(k)).strip() not in ("", "nan")
        for k in ("valid_month_start", "valid_year_start",
                  "valid_month_end", "valid_year_end")
    )


def is_drought_resolved(row) -> bool:
    """Drought analogue of is_resolved: dated, or flagged not-a-drought.

    not_drought marks a 'Drought'-typed allocation with no meteorological drought
    behind it (e.g. the 2008 food-price-crisis allocations) — it will never get a
    valid period."""
    return has_valid_period(row) or bool(row.get("not_drought"))


def ensure_tables() -> None:
    with stratus.get_engine(write=True).begin() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.cerf_supplement (
                application_code   text PRIMARY KEY,
                not_tc             boolean,
                not_drought        boolean,
                valid_month_start  integer,
                valid_year_start   integer,
                valid_month_end    integer,
                valid_year_end     integer,
                confidence         double precision,
                notes              text,
                updated_at         timestamptz
            )"""))
        # migrations for tables created before these columns existed
        conn.execute(text(f"""
            ALTER TABLE {SCHEMA}.cerf_supplement
            ADD COLUMN IF NOT EXISTS confidence double precision"""))
        conn.execute(text(f"""
            ALTER TABLE {SCHEMA}.cerf_supplement
            ADD COLUMN IF NOT EXISTS not_drought boolean"""))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.cerf_allocation_storm (
                application_code text NOT NULL,
                sid              text NOT NULL,
                updated_at       timestamptz,
                PRIMARY KEY (application_code, sid)
            )"""))


def load_supplemental() -> pd.DataFrame:
    """Read the two tables back into the legacy DataFrame shape."""
    engine = stratus.get_engine()
    with engine.connect() as conn:
        supp = pd.read_sql(text(f"SELECT * FROM {SCHEMA}.cerf_supplement"), conn)
        links = pd.read_sql(
            text(f"SELECT application_code, sid FROM {SCHEMA}.cerf_allocation_storm"), conn)
    for _col in ("confidence", "not_drought"):  # table may predate these columns
        if _col not in supp.columns:
            supp[_col] = None

    sids_by_code: dict[str, list[str]] = {}
    for _, r in links.iterrows():
        sids_by_code.setdefault(r["application_code"], []).append(r["sid"])

    codes = set(supp["application_code"]) | set(sids_by_code)
    supp_by_code = {r["application_code"]: r for _, r in supp.iterrows()}
    rows = []
    for code in codes:
        s = supp_by_code.get(code)
        rows.append({
            "ApplicationCode": code,
            "sids": encode_sids(sids_by_code.get(code, [])),
            "not_tc": (bool(s["not_tc"]) if s is not None and pd.notna(s["not_tc"]) else None),
            "not_drought": (bool(s["not_drought"]) if s is not None and pd.notna(s["not_drought"]) else None),
            "valid_month_start": s["valid_month_start"] if s is not None else None,
            "valid_year_start": s["valid_year_start"] if s is not None else None,
            "valid_month_end": s["valid_month_end"] if s is not None else None,
            "valid_year_end": s["valid_year_end"] if s is not None else None,
            "confidence": s["confidence"] if s is not None else None,
            "notes": s["notes"] if s is not None else None,
            "updated_at": s["updated_at"] if s is not None else None,
        })
    return pd.DataFrame(rows, columns=_COLUMNS)


def save_supplemental(df: pd.DataFrame) -> None:
    """Transactional full replace of both tables from the DataFrame."""
    def _i(v):
        return int(v) if pd.notna(v) and str(v).strip() not in ("", "nan") else None

    with stratus.get_engine(write=True).begin() as conn:
        conn.execute(text(f"DELETE FROM {SCHEMA}.cerf_allocation_storm"))
        conn.execute(text(f"DELETE FROM {SCHEMA}.cerf_supplement"))
        for _, r in df.iterrows():
            code = r["ApplicationCode"]
            ts = r.get("updated_at") or datetime.now(timezone.utc)
            conf = r.get("confidence")
            conn.execute(text(f"""
                INSERT INTO {SCHEMA}.cerf_supplement
                  (application_code, not_tc, not_drought, valid_month_start,
                   valid_year_start, valid_month_end, valid_year_end, confidence,
                   notes, updated_at)
                VALUES (:c, :nt, :nd, :ms, :ys, :me, :ye, :cf, :n, :ts)"""), {
                "c": code,
                "nt": bool(r["not_tc"]) if pd.notna(r.get("not_tc")) else None,
                "nd": bool(r["not_drought"]) if pd.notna(r.get("not_drought")) else None,
                "ms": _i(r.get("valid_month_start")), "ys": _i(r.get("valid_year_start")),
                "me": _i(r.get("valid_month_end")), "ye": _i(r.get("valid_year_end")),
                "cf": float(conf) if pd.notna(conf) else None,
                "n": (r.get("notes") or None), "ts": ts,
            })
            for sid in decode_sids(r["sids"]):
                conn.execute(text(f"""
                    INSERT INTO {SCHEMA}.cerf_allocation_storm (application_code, sid, updated_at)
                    VALUES (:c, :s, :ts)"""), {"c": code, "s": sid, "ts": ts})


def upsert_annotation(supp_df: pd.DataFrame, app_code: str, annotation: dict) -> pd.DataFrame:
    annotation["ApplicationCode"] = app_code
    annotation["updated_at"] = datetime.now(timezone.utc)
    filtered = supp_df[supp_df["ApplicationCode"] != app_code]
    return pd.concat([filtered, pd.DataFrame([annotation])], ignore_index=True)


def remove_annotation(supp_df: pd.DataFrame, app_code: str) -> pd.DataFrame:
    return supp_df[supp_df["ApplicationCode"] != app_code].reset_index(drop=True)
