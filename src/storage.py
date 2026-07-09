import json
from datetime import datetime, timezone

import pandas as pd
import ocha_stratus as stratus

BLOB_NAME = "cerf/cerf_supplemental_data.parquet"
STAGE = "dev"
CONTAINER = "global"

_COLUMNS = [
    "ApplicationCode",  # unique key (ApplicationID is NOT unique in the feed)
    "sids",  # JSON-encoded list of IBTrACS SIDs, e.g. '["sid1", "sid2"]'
    "not_tc",  # True = storm allocation that is definitely NOT a tropical cyclone
    "valid_month_start",
    "valid_year_start",
    "valid_month_end",
    "valid_year_end",
    "notes",
    "updated_at",
]

# a fully-resolved row has either storm(s) assigned or is flagged not-a-TC
def is_resolved(row) -> bool:
    return bool(decode_sids(row.get("sids"))) or bool(row.get("not_tc"))


def encode_sids(sids: list[str] | None) -> str | None:
    """List of SIDs -> JSON string (or None if empty)."""
    sids = [s for s in (sids or []) if s]
    return json.dumps(sids) if sids else None


def decode_sids(value) -> list[str]:
    """JSON string (or legacy scalar) -> list of SIDs."""
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
        return [s]  # legacy single-SID string


def _migrate(df: pd.DataFrame) -> pd.DataFrame:
    """Bring an older-schema frame up to date in-memory."""
    if "sids" not in df.columns and "sid" in df.columns:
        df = df.copy()
        df["sids"] = df["sid"].apply(lambda s: encode_sids([s]) if pd.notna(s) else None)
        df = df.drop(columns=["sid"])
    for col in _COLUMNS:
        if col not in df.columns:
            df[col] = None
    return df[_COLUMNS]


def load_supplemental() -> pd.DataFrame:
    try:
        return _migrate(
            stratus.load_parquet_from_blob(BLOB_NAME, stage=STAGE, container_name=CONTAINER)
        )
    except Exception:
        return pd.DataFrame(columns=_COLUMNS)


def save_supplemental(df: pd.DataFrame) -> None:
    stratus.upload_parquet_to_blob(df, BLOB_NAME, stage=STAGE, container_name=CONTAINER)


def upsert_annotation(supp_df: pd.DataFrame, app_code: str, annotation: dict) -> pd.DataFrame:
    annotation["ApplicationCode"] = app_code
    annotation["updated_at"] = datetime.now(timezone.utc)
    filtered = supp_df[supp_df["ApplicationCode"] != app_code]
    return pd.concat([filtered, pd.DataFrame([annotation])], ignore_index=True)


def remove_annotation(supp_df: pd.DataFrame, app_code: str) -> pd.DataFrame:
    return supp_df[supp_df["ApplicationCode"] != app_code].reset_index(drop=True)
