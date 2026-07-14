You are dating historical CERF (Central Emergency Response Fund) drought
allocations: for each one, identify the **valid period** — the months in which
the actual **meteorological drought** (the rainfall deficit) happened.

This is NOT the allocation date and NOT the response period. A CERF drought
allocation typically follows one or more **failed rainy seasons**, often up to a
year earlier (e.g. an allocation in March 2017 responding to failed Oct–Dec 2016
*deyr* rains → valid period Oct 2016 – Dec 2016). Anticipatory-action
allocations can be endorsed *before or during* the forecast failed season, so
the valid period may extend past the allocation date.

## Task

1. Read `claude_work/unresolved_droughts.json`. It contains a list of
   `allocations`, each with: `code`, `country`, `year`, `amount`, `type`,
   `title`, `endorsement_date`, `summary`, `overview`, `rationale`,
   `projects` (the project titles funded under the allocation, scraped from
   the CERF website — often the only text that names the driver or season
   when the narratives are empty), `current_period`, and `user_comments`.

   Some allocations carry a `current_period` (already dated). If it's non-null,
   this is an **existing entry being reviewed** — only act if the
   `user_comments` tell you to. If the human confirms it's correct, return the
   same period at `confidence` ≥ 0.9 (re-affirms it and closes the issue). If
   they correct it, return the corrected period. If there are no comments,
   return low confidence and change nothing.

   **`user_comments` are authoritative human guidance** left on the GitHub
   issue. If present, follow them over your own reasoning:
   - If the human states the period ("Oct 2020 – Mar 2021", "the failed 2016
     deyr"), use it and set `confidence` ≥ 0.9.
   - If the human says to leave it / it's unclear, return low confidence so it
     stays open.

2. For each allocation, work out the rainfall-deficit period:
   - **The narratives usually name the failed season(s)** — read `summary` /
     `overview` / `rationale` first ("consecutive failed *deyr* and *gu*
     rains", "poor 2015/16 El Niño-affected season", "third failed *belg*").
     **Also read the `projects` titles** — they often name the driver or the
     affected season when the narratives are empty ("assist households
     affected by the 2011 rain failure in ...", or conversely "...affected by
     the conflict and the soaring prices" → not a drought at all).
   - Convert named seasons to calendar months using the country's climatology,
     e.g. Horn of Africa: *gu*/long rains ≈ Mar–May, *deyr*/*hagaya*/short
     rains ≈ Oct–Dec; Ethiopia *belg* ≈ Feb–May, *kiremt*/*meher* rains ≈
     Jun–Sep; Sahel rainy season ≈ Jun–Sep; Southern Africa rainy season ≈
     Nov–Mar (spans the year boundary). Use **web search** when the narrative
     is vague or you need to confirm which season failed in that specific year.
   - When **consecutive seasons** failed, the valid period spans from the start
     of the first failed season to the end of the last one (may include the
     normal-rain gap between them). Cap the period at 24 months — for a long
     multi-year drought, prefer the most recent failed season(s) that the
     allocation actually responded to.
   - Report the period as start month/year → end month/year (calendar months,
     1–12; separate years so the period can span a year boundary).
   - **Fallback when no season is named anywhere**: if your research confirms
     the crisis genuinely was a drought but no source names the failed
     season, date it to the country's/region's **most recent climatological
     rainy season preceding the allocation** (a March-2011 Djibouti drought
     allocation → the failed Jul–Sep 2010 *karan/karma* rains and/or the
     Oct–Dec short rains). For a country with ONE clear rainy season this
     supports confidence up to ~0.8; where seasons are bimodal and you can't
     tell which failed, keep confidence lower and say so.
   - **`not_drought`** — if the evidence shows there was **no meteorological
     drought behind the allocation at all** (the feed's "Drought" type is
     wrong: global food-price crisis, conflict/displacement, economic
     collapse), set `"not_drought": true` with no period. **Use this
     conservatively**: you must have actively looked for a drought (narratives,
     projects, web) and found the driver to be something else — "I couldn't
     find the season" is NOT `not_drought`, it's low confidence. It is only
     auto-applied at confidence ≥ 0.9.

3. Assign a `confidence` from 0.0 to 1.0. Only be ≥ 0.8 when the narrative (or
   your research) clearly identifies the failed season(s) and the timing fits
   the allocation. Use lower confidence when the narrative is vague about
   timing, seasons conflict, or the drought is chronic/multi-year with no clear
   anchor — say briefly what you'd need to confirm; it will be left for a human
   to check rather than written automatically.

## Output

Write `claude_work/drought_matches.json`: a JSON array, one object per
allocation you have an opinion on (omit ones you can't say anything useful
about):

```json
[
  {
    "code": "17-RR-SOM-00000",
    "valid_month_start": 10,
    "valid_year_start": 2016,
    "valid_month_end": 12,
    "valid_year_end": 2016,
    "confidence": 0.9,
    "reasoning": "One or two sentences: which season(s) failed, and the evidence."
  },
  {
    "code": "08-RR-XXX-00000",
    "not_drought": true,
    "confidence": 0.95,
    "reasoning": "Why this is not a meteorological drought (what the real driver was)."
  }
]
```

Rules:
- Months are 1–12; the end must not be before the start; the period must be
  ≤ 24 months and within 2 years of the allocation year.
- Exactly one of a full period or `not_drought: true` per entry — never both.
- `reasoning` should name the failed season(s) (or the real driver) — it
  becomes the row's `notes`.
- Keep `reasoning` concise. Do not write any other files.
