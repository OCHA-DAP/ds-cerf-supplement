You are matching historical CERF (Central Emergency Response Fund) storm
allocations to the specific tropical cyclone(s) they responded to, identified
by their IBTrACS Storm ID (SID).

## Task

1. Read `claude_work/unresolved.json`. It contains a list of `allocations`,
   each with: `code`, `country`, `year`, `amount`, `type`, `title`, `summary`,
   and `candidates` (IBTrACS storms within ±1 year of the allocation year, each
   with `sid`, `name`, `season`, `basin`).

2. For each allocation, decide which tropical cyclone(s) it responded to:
   - Use the title and summary first. Use **web search** to confirm which
     specific storm hit that country around that date, especially for
     ambiguous ("Cyclones", "six typhoons") or anticipatory-action allocations.
   - The SID(s) you output **must come from that allocation's `candidates`
     list** (that's the IBTrACS universe we can store). If the correct storm is
     not among the candidates (e.g. too recent to be archived), return an empty
     `sids` list with low confidence.
   - An allocation can map to **multiple** storms (e.g. a season that hit a
     country with several cyclones, or a named pair).
   - If the allocation is a storm but **definitely not a tropical cyclone**
     (tornado, winter storm, extratropical storm, purely inland flooding in a
     country outside any TC basin), set `not_tc: true` and leave `sids` empty.

3. Assign a `confidence` from 0.0 to 1.0. Only be ≥ 0.8 when you are genuinely
   sure (the storm name matches, the timing and country fit). When unsure, use
   a lower value and briefly say what you'd need to confirm — it will be left
   for a human to check rather than written automatically.

## Output

Write `claude_work/matches.json`: a JSON array, one object per allocation you
have an opinion on (you may omit allocations you can't say anything useful
about):

```json
[
  {
    "code": "23-RR-XXX-00000",
    "sids": ["2023036S12117"],
    "not_tc": false,
    "confidence": 0.9,
    "reasoning": "One or two sentences: which storm, why, what evidence."
  }
]
```

Rules:
- `sids` must be a subset of that allocation's `candidates` SIDs (or empty).
- Exactly one of `sids` (non-empty) or `not_tc: true` should be set when you're
  confident; both empty/false means "unsure, leave for review".
- Keep `reasoning` concise. Do not write any other files.
