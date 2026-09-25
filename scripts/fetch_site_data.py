"""
Deploy-side counterpart of publish_site_data.py: download the site data the
daily Databricks job parked on the dev blob into site/data.json. Run by
deploy-site.yml on the GitHub runner (needs DSCI_AZ_BLOB_DEV_SAS).
"""
import json
import sys
from pathlib import Path

import ocha_stratus as stratus

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.site_blob import BLOB_CONTAINER, BLOB_NAME, BLOB_STAGE  # noqa: E402

OUT = Path(__file__).parent.parent / "site" / "data.json"


def main():
    payload = stratus.load_blob_data(BLOB_NAME, stage=BLOB_STAGE, container_name=BLOB_CONTAINER)
    if isinstance(payload, str):
        payload = payload.encode()
    doc = json.loads(payload)  # fail loudly on a truncated / empty upload
    OUT.write_bytes(payload)
    print(f"wrote {OUT} ({len(payload):,} bytes, {len(doc.get('rows', []))} rows, generated {doc.get('generated_at')})")


if __name__ == "__main__":
    main()
