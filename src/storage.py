from datetime import datetime, timezone

import pandas as pd
import ocha_stratus as stratus

BLOB_NAME = "cerf/cerf_supplemental_data.parquet"
STAGE = "dev"
CONTAINER = "global"

_COLUMNS = [
    "ApplicationID",
    "sid",
    "valid_month_start",
    "valid_year_start",
    "valid_month_end",
    "valid_year_end",
    "notes",
    "updated_at",
]


def load_supplemental() -> pd.DataFrame:
    try:
        df = stratus.load_parquet_from_blob(BLOB_NAME, stage=STAGE, container_name=CONTAINER)
        # Ensure all expected columns exist (schema migration safety)
        for col in _COLUMNS:
            if col not in df.columns:
                df[col] = None
        return df[_COLUMNS]
    except Exception:
        return pd.DataFrame(columns=_COLUMNS)


def save_supplemental(df: pd.DataFrame) -> None:
    stratus.upload_parquet_to_blob(df, BLOB_NAME, stage=STAGE, container_name=CONTAINER)


def upsert_annotation(supp_df: pd.DataFrame, app_id: str, annotation: dict) -> pd.DataFrame:
    annotation["ApplicationID"] = app_id
    annotation["updated_at"] = datetime.now(timezone.utc)
    filtered = supp_df[supp_df["ApplicationID"] != app_id]
    return pd.concat([filtered, pd.DataFrame([annotation])], ignore_index=True)


def remove_annotation(supp_df: pd.DataFrame, app_id: str) -> pd.DataFrame:
    return supp_df[supp_df["ApplicationID"] != app_id].reset_index(drop=True)
