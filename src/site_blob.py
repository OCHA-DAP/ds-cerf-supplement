"""Where the Databricks job leaves site/data.json for the GitHub Pages deploy.

The Pages workflow runs on a GitHub runner, which cannot reach the DB any
more, so the daily job exports the site data and parks it on the dev blob;
the deploy workflow just downloads it (see scripts/publish_site_data.py and
scripts/fetch_site_data.py).
"""

BLOB_STAGE = "dev"
BLOB_CONTAINER = "projects"
BLOB_NAME = "ds-cerf-supplement/site/data.json"
