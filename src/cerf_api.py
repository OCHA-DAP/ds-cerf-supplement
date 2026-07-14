import xml.etree.ElementTree as ET
from functools import lru_cache

import pandas as pd
import requests

CERF_API_URL = "https://cerfgms-webapi.unocha.org/v1/application/All.xml"

_FIELDS = [
    "ApplicationID",
    "ApplicationCode",
    "ApplicationTitle",
    "CountryName",
    "Year",
    "EmergencyTypeName",
    "TotalAmountApproved",
    "CN_ERC_EndorsementDate",
    "WindowFullName",
    "AllocationStatus",
    "CN_Summary",
    "OverviewoftheHumanitarianSituation",
    "RationaleforCERFAllocation",
]


@lru_cache(maxsize=1)
def fetch_cerf_allocations() -> pd.DataFrame:
    resp = requests.get(CERF_API_URL, timeout=120)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    rows = []
    for app_el in root.findall("application"):
        row: dict = {}
        for field in _FIELDS:
            el = app_el.find(field)
            row[field] = el.text.strip() if el is not None and el.text else None
        rows.append(row)
    df = pd.DataFrame(rows)
    df["Year"] = pd.to_numeric(df["Year"], errors="coerce").astype("Int64")
    df["TotalAmountApproved"] = pd.to_numeric(
        df["TotalAmountApproved"], errors="coerce"
    )
    return df


# cerf.un.org serves a "Projects included in this allocation" table (organization,
# project title, code, amount) that the OneGMS All.xml feed doesn't carry. Project
# titles often name the driver ("...affected by the conflict and the soaring
# prices...") or the storm — useful when the narratives are empty (pre-2013).
# The site 403s non-browser user agents, hence the UA header.
_PAGE_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
_PROJECT_ROW_RE = None  # compiled lazily


def fetch_project_titles(code: str, year) -> list[str]:
    """Project titles for an allocation, scraped from its cerf.un.org summary page.

    Returns [] on any failure — enrichment only, never a hard dependency."""
    import re

    global _PROJECT_ROW_RE
    if _PROJECT_ROW_RE is None:
        _PROJECT_ROW_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
    url = f"https://cerf.un.org/what-we-do/allocation/{year}/summary/{code}"
    try:
        resp = requests.get(url, headers=_PAGE_UA, timeout=30)
        resp.raise_for_status()
    except requests.RequestException:
        return []
    m = re.search(r"Projects included in this allocation(.*?)</table>",
                  resp.text, re.S)
    if not m:
        return []
    cells = [re.sub(r"<[^>]+>|\s+", " ", c).strip()
             for c in _PROJECT_ROW_RE.findall(m.group(1))]
    # cell layout varies (org / title / Read more / amount / code ...) — keep the
    # cells that look like project titles rather than relying on column position
    _noise = re.compile(r"^(US ?\$|Read more$|\d{2}-[A-Z]{2,4}-\d+|[A-Z]{2,6}-\d)")
    import html as _html
    out, seen = [], set()
    for c in cells:
        if len(c) >= 25 and not _noise.match(c) and c not in seen:
            seen.add(c)
            out.append(_html.unescape(c))
    return out


STORM_KEYWORDS = {"cyclone", "hurricane", "typhoon", "tropical storm", "storm"}
DROUGHT_KEYWORDS = {"drought"}


def classify_type(emergency_type: str | None) -> str:
    if not emergency_type:
        return "Other"
    et = emergency_type.lower()
    if any(k in et for k in STORM_KEYWORDS):
        return "Storm"
    if any(k in et for k in DROUGHT_KEYWORDS):
        return "Drought"
    return "Other"
