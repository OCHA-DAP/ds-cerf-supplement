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
