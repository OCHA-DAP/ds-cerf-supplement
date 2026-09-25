"""
Last step of the daily Databricks job: park site/data.json on the dev blob and
ask GitHub to deploy the Pages site from it.

  1. upload site/data.json -> projects/ds-cerf-supplement/site/data.json (dev blob)
  2. POST a workflow_dispatch for deploy-site.yml (needs GITHUB_TOKEN with
     actions:write on the repo; skipped with a warning when absent — the
     workflow's daily 08:00 UTC schedule is the backstop either way).
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

import ocha_stratus as stratus

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.site_blob import BLOB_CONTAINER, BLOB_NAME, BLOB_STAGE  # noqa: E402

DATA = Path(__file__).parent.parent / "site" / "data.json"
REPO = os.getenv("GITHUB_REPOSITORY", "OCHA-DAP/ds-cerf-supplement")
WORKFLOW = "deploy-site.yml"


def main():
    payload = DATA.read_bytes()
    rows = len(json.loads(payload).get("rows", []))
    stratus.upload_blob_data(
        payload,
        BLOB_NAME,
        stage=BLOB_STAGE,
        container_name=BLOB_CONTAINER,
        content_type="application/json",
    )
    print(f"uploaded {DATA.name} ({len(payload):,} bytes, {rows} rows) -> {BLOB_STAGE}:{BLOB_CONTAINER}/{BLOB_NAME}")

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("WARNING: GITHUB_TOKEN not set — not dispatching deploy-site (daily schedule will pick it up)")
        return
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches",
        data=json.dumps({"ref": "main"}).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        print(f"dispatched {WORKFLOW} on {REPO}: HTTP {resp.status}")


if __name__ == "__main__":
    main()
