# RUNBOOK — Person A: "The Brain" (LLM Interpretation Engine)

You own: `app/llm.py`, the prompt inside it, and (optional new file) `app/fallback_parser.py`.
You do NOT touch: anything else. `app/schemas.py` and `docs/CONTEXT.md` are frozen contracts.

## Your core feature
Turn 1-3 plain-English operator notes into validated directive primitives — the single
highest-scoring component (25 pts). Paraphrase robustness is where other teams die.

## Phase 1 — Key live (0:00–0:20) — DO THIS FIRST
1. Create a free Groq key: https://console.groq.com → API Keys.
2. Add to `.env` (never commit): `GROQ_API_KEY=gsk_...`
3. Also create an OpenRouter key (fallback): `OPENROUTER_API_KEY=sk-or-...`
4. Smoke test from the repo venv:
   ```bash
   .venv/Scripts/python -c "
   from openai import OpenAI
   c = OpenAI(base_url='https://api.groq.com/openai/v1', api_key='__YOUR_KEY__')
   r = c.chat.completions.create(model='llama-3.3-70b-versatile',
       messages=[{'role':'user','content':'Reply with JSON {\"ok\":true}'}],
       response_format={'type':'json_object'}, temperature=0)
   print(r.choices[0].message.content)"
   ```
   If `openai` package missing: `pip install openai` (add to requirements.txt via D or yourself —
   requirements.txt is shared, coordinate in chat, one-line change).

## Phase 2 — Engine build (0:20–1:30)

### Call design (all inside `app/llm.py`)
- **One batched call per request**: all notes together, ask for one object per note keyed by `note_index`.
- `temperature=0`, `response_format={"type":"json_object"}`, timeout ~8s, **one retry**.
- Pass `battery.capacity_kwh` in the prompt so %-of-capacity conversions have context.
- Cache results keyed on hash of (note text) — judges may resend identical notes.

### CRITICAL: LLM emits primitives, code builds the directive
Never let the LLM output `hours` arrays or final `factor` directly. Ask for:
```json
{"interpretations": [{
  "note_index": 0,
  "directive_type": "solar_reduction",
  "window": {"start_hour": 13, "end_hour": 15},
  "reduction_type": "remaining | reduced_by",
  "percent": 20,
  "value": {"number": 120, "unit": "kwh | mwh | percent_of_capacity"},
  "reasoning": "one short sentence"
}]}
```
Then in code:
- `hours = list(range(start, end))`; if `end < start` (midnight wrap): `sorted(list(range(start,24)) + list(range(0,end)))`
- `factor = pct/100 if reduction_type=="remaining" else 1 - pct/100`, clamp to [0,1]
- unit conversion: `mwh × 1000`, `percent_of_capacity × battery.capacity_kwh`

### System prompt must encode (put these as explicit rules + few-shots)
| Trap | Rule |
|---|---|
| "1 PM to 3 PM" / "13:00–15:00" / "one until three" | start=13, end=15 (end-exclusive) |
| "noon"=12, "midnight" start=0, "midnight" end=24 | |
| "after 8 PM" / "rest of the day" | 20 / 24 |
| "all day" | 0 / 24 |
| "at 3 PM" (single hour) | 15 / 16 |
| "drop **to** 20%" | remaining, factor 0.2 |
| "drop **by** 20%" / "80% reduction" | reduced_by → factor 0.2 |
| "halved" | 0.5 |
| "no solar" / "panels offline" | factor 0.0 |
| "no more than / cap / limit" on grid | `max_grid_window` |
| "at least / no lower than / keep ≥" on battery | `minimum_battery_reserve` |
| "don't draw from storage / freeze battery" | `no_discharge_window` |
| "don't charge / charger maintenance" | `no_charge_window` |
| Distractors (menus, events, "next week", "tariff is high in evening", vague "solar may be weaker") | `no_op` — NEVER convert unsupported topics into a supported type |

Include 15-25 few-shot examples: every paraphrase above + 5-8 distractors that *mention*
solar/battery but are not instructions. Each note maps to EXACTLY ONE directive — choose the
most specific type the wording supports.

### Fallback chain (safe failure — required by spec)
1. Parse + validate LLM JSON.
2. Fail → retry once, include the validation error in the retry prompt.
3. Fail again → deterministic regex parser (`app/fallback_parser.py`): time ranges,
   percentages, keywords (charge/reserve/grid/solar). Keep it simple, ~50 lines.
4. Still unclassifiable → `no_op` with explanation "could not parse note".
Never crash. Never invent a directive. Log every fallback.

## Phase 2:30–2:45
Tune the prompt against C's failing cases from `tests/testbank.json`. Only change the prompt
and few-shots — not the guardrail contract.

## Done when
- [ ] Real Groq call produces valid interpretations for all 10 public-sample note sets
- [ ] ≥90% accuracy on C's test bank (C prints the score)
- [ ] Kill the key (wrong key) → request still returns a valid response (fallback chain works)
- [ ] Latency of the LLM step < 5s (measure it)

## Pitfalls
- Do not hardcode public-sample phrasings — hidden notes paraphrase.
- Do not skip the retry-on-validation-error step; it's cheap and saves misformatted runs.
- Never print the API key in logs or errors.
