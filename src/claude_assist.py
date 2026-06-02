import json

import anthropic

CONFIDENCE_THRESHOLD = 0.75

_SYSTEM = (
    "You are an expert on CERF (Central Emergency Response Fund) humanitarian "
    "allocations and natural disasters. You have deep knowledge of named tropical "
    "cyclones, their IBTrACS identifiers, and historical drought patterns worldwide."
)

_PROMPT_TEMPLATE = """Given this CERF allocation, identify:
1. For storms/cyclones/hurricanes/typhoons: the IBTrACS SID. IBTrACS SIDs follow the format YYYYJJJLATLON (e.g. "2023325S10086") where JJJ is the Julian day of first position, or occasionally a WMO ID like "2023AL09".
2. For droughts: which calendar months (1–12) had the actual rainfall deficit driving the crisis, and which year those months occurred in.
3. Attempt both if the allocation could involve either.

Allocation:
- Title: {title}
- Country: {country}
- Year: {year}
- Emergency Type: {emergency_type}
- Summary: {summary}
- Humanitarian Situation: {situation}
- Rationale: {rationale}

Respond with ONLY a JSON object, no other text:
{{
  "sid": "<IBTrACS SID string, or null if not a storm or unknown>",
  "valid_months": [<integers 1–12, empty list if not a drought or unknown>],
  "valid_year": <integer year or null>,
  "confidence": <float 0.0–1.0 reflecting your overall certainty>,
  "reasoning": "<one or two sentences explaining your answer and confidence>"
}}"""


def get_suggestion(allocation: dict) -> dict:
    client = anthropic.Anthropic()
    prompt = _PROMPT_TEMPLATE.format(
        title=allocation.get("ApplicationTitle") or "N/A",
        country=allocation.get("CountryName") or "N/A",
        year=allocation.get("Year") or "N/A",
        emergency_type=allocation.get("EmergencyTypeName") or "N/A",
        summary=allocation.get("CN_Summary") or "N/A",
        situation=allocation.get("OverviewoftheHumanitarianSituation") or "N/A",
        rationale=allocation.get("RationaleforCERFAllocation") or "N/A",
    )
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=512,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
    return json.loads(text)
