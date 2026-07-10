"""
One-off: migrate the supplemental data from the blob parquet into the dev DB
(aa.cerf_supplement + aa.cerf_allocation_storm). After this the DB is the source
of truth and the blob is no longer used.

Run with --write.
"""

import argparse
import os
import sys
from pathlib import Path

import pandas as pd
import ocha_stratus as stratus

os.environ.setdefault("PGSSLMODE", "require")
sys.path.insert(0, str(Path(__file__).parent.parent))
import src.storage as st  # noqa: E402

OLD_BLOB = "cerf/cerf_supplemental_data.parquet"


def main(write: bool):
    df = stratus.load_parquet_from_blob(OLD_BLOB, stage="dev", container_name="global")
    # normalize to the current column shape (older blobs used `sid`)
    if "sids" not in df.columns and "sid" in df.columns:
        df["sids"] = df["sid"].apply(lambda s: st.encode_sids([s]) if pd.notna(s) else None)
    for col in st._COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[st._COLUMNS]
    n_sid = sum(1 for _, r in df.iterrows() if st.decode_sids(r["sids"]))
    n_nottc = sum(1 for _, r in df.iterrows() if bool(r["not_tc"]))
    print(f"Blob has {len(df)} rows ({n_sid} with SID, {n_nottc} not_tc)")

    if not write:
        print("(dry run — pass --write to create tables and load)")
        return

    st.ensure_tables()
    st.save_supplemental(df)
    back = st.load_supplemental()
    print(f"DB now has {len(back)} rows "
          f"({sum(1 for _, r in back.iterrows() if st.decode_sids(r['sids']))} with SID, "
          f"{sum(1 for _, r in back.iterrows() if bool(r['not_tc']))} not_tc)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    main(ap.parse_args().write)
