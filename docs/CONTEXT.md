# GridWise Challenge — Canonical Context

This file is the single source of truth for this repo's requirements. Read it before writing or editing any code. It is a condensed, field-accurate extraction of the official Problem Statement and Participant Guide — treat field names, types, and directive shapes below as exact, not paraphrased.

## What this service does
Receives a 24-hour energy scenario + 1-3 natural-language operator notes. Uses an LLM to interpret each note into a structured directive. Validates that output deterministically. Applies valid directives to a linear-programming optimizer. Replays the resulting schedule against every extracted directive as a final sanity check. Returns a valid, cost-minimized 24-hour schedule plus the interpretation trace.

Pipeline: LLM (untrusted, interprets language) -> deterministic guardrails (validate structure) -> optimizer (solves math) -> replay/sanity check (verify every extracted directive actually holds in the plan) -> response.

## Endpoints (exact names, judge only calls these two)
- `GET /health` -> `200 {"status": "ok"}` within 60s of service start.
- `POST /optimize-energy` -> accepts one scenario JSON, returns one interpretation+plan JSON.
- No login, dashboard, VPN, or manual approval may ever be required to reach these.
- Per-request timeout: must complete within 30s. p95 <= 5s scores full latency credit.
- 400 for malformed JSON, 500 only as a controlled error (never leak secrets/stack traces).

## Request schema (`POST /optimize-energy` body)
```json
{
  "scenario_id": "string",
  "operator_notes": ["1 to 3 non-empty strings"],
  "hours": [
    {"hour": 0, "demand_kwh": 180.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 7.0}
    // exactly 24 entries, hour 0..23, each unique
  ],
  "battery": {
    "capacity_kwh": 500.0,
    "initial_energy_kwh": 200.0,
    "minimum_energy_kwh": 50.0,
    "max_charge_kwh_per_hour": 100.0,
    "max_discharge_kwh_per_hour": 100.0
  }
}
```

## Supported directive types (ONLY these six — never invent others)

| directive_type | meaning | required structured_adjustment |
|---|---|---|
| solar_reduction | reduce usable solar in listed hours | `{"hours":[...], "factor": number}` — factor is the fraction of solar that REMAINS (an 80% reduction = factor 0.2) |
| minimum_battery_reserve | battery must stay >= this level in listed hours | `{"hours":[...], "minimum_energy_kwh": number}` |
| no_charge_window | charging forbidden in listed hours | `{"hours":[...]}` |
| no_discharge_window | discharging forbidden in listed hours | `{"hours":[...]}` |
| max_grid_window | grid import capped in listed hours | `{"hours":[...], "max_grid_kwh": number}` |
| no_op | note doesn't affect the schedule | `null` |

Rules:
- Time windows are whole hours, start inclusive / end exclusive: "1 PM to 3 PM" -> hours `[13, 14]`.
- `hours` arrays must be unique integers 0-23, ascending order.
- Every non-`no_op` directive MUST have `applies: true`. `no_op` is the ONLY directive type allowed `applies: false`.
- `factor` for solar_reduction must be within [0, 1].
- Hidden test notes will paraphrase these same six directives in different wording — do not hardcode phrase-matching. The LLM call is mandatory and must be the thing producing the interpretation, not just cosmetic text/plan_summary.
- The LLM must never invent demand, tariff, battery limits, or a directive type outside the six above.

## Response schema (`POST /optimize-energy`)
```json
{
  "scenario_id": "must echo the request's scenario_id",
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
      "explanation": "short free-text, not scored byte-for-byte"
    }
    // exactly one entry per operator_notes item, IN note_index ORDER 0..N-1, no gaps/dupes
  ],
  "hourly_plan": [
    {
      "hour": 0,
      "grid_kwh": 0.0,
      "solar_used_kwh": 0.0,
      "battery_action": "charge | discharge | idle",
      "battery_kwh": 0.0,
      "battery_energy_after_kwh": 0.0
    }
    // exactly 24 entries, hour 0..23 unique
  ],
  "total_grid_kwh": 0.0,
  "total_cost_bdt": 0.0,
  "peak_grid_kwh": 0.0,
  "plan_summary": "short human-readable string"
}
```
`total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh` MUST be recalculable from `hourly_plan` alone — the judge recomputes them and checks they match within tolerance.

## Optimization / energy rules (hard constraints — a cheap plan that breaks these scores ZERO)
- Objective: minimize `sum(grid_kwh[h] * tariff_bdt_per_kwh[h])` for h in 0..23, AFTER applying all valid directives.
- Battery: `E_after = E_before + battery_kwh` (charge) / `E_before - battery_kwh` (discharge) / unchanged & `battery_kwh=0` (idle).
- Bounds: `minimum_energy_kwh <= E_after <= capacity_kwh` for every hour (use the directive's reserve if higher than base for that hour).
- Rate limits: `battery_kwh <= max_charge_kwh_per_hour` when charging, `<= max_discharge_kwh_per_hour` when discharging.
- Solar: `0 <= solar_used_kwh <= effective_solar_kwh[h]` (effective = base solar * any active solar_reduction factor for that hour; unused solar is curtailed, no export/sell-back).
- Energy balance every hour: `grid_kwh + solar_used_kwh + battery_discharge_kwh == demand_kwh + battery_charge_kwh`.
- End-of-day neutrality: final `battery_energy_after_kwh` (hour 23) MUST equal `initial_energy_kwh`. This is a hard constraint in the LP, not a preference.
- `no_charge_window` -> force `battery_kwh = 0` for charge action in those hours. `no_discharge_window` -> same for discharge.
- `max_grid_window` -> `grid_kwh[h] <= max_grid_kwh` for those hours.
- Numeric tolerance for equality/bound checks: 0.01 kWh / 0.01 BDT.

## Guardrail requirements (deterministic code, between LLM and optimizer)
- Validate LLM JSON against a strict schema: exactly one entry per note, `note_index` covers 0..N-1 with no gaps/dupes, `directive_type` in the six allowed values, `hours` unique ascending ints 0-23, numeric fields finite and non-negative, `factor` in [0,1], `minimum_energy_kwh` for `minimum_battery_reserve` finite/non-negative AND not exceeding the scenario's `battery.capacity_kwh`, `max_grid_kwh` finite and non-negative, `applies`/`directive_type`/`structured_adjustment` consistency per the no_op rule above.
- If the LLM returns malformed/unparseable/out-of-schema output: do NOT crash, do NOT invent a directive. Fail safe — treat that note as unresolved (log it, and prefer marking it `no_op` with an explanation noting the parse failure) so the service stays up and returns a valid response.
- Never let raw LLM text bypass validation and reach the optimizer directly.

## Final replay / sanity check (required — do not skip)
After the optimizer produces `hourly_plan`, before returning the response, replay it in code against every `directive_interpretation` entry with `applies: true` and confirm the plan actually obeys it:
- `solar_reduction`: `solar_used_kwh[h] <= base_solar_kwh[h] * factor` for each listed hour.
- `minimum_battery_reserve`: `battery_energy_after_kwh[h] >= minimum_energy_kwh` for each listed hour.
- `no_charge_window`: `battery_action != "charge"` (or `battery_kwh == 0`) for each listed hour.
- `no_discharge_window`: `battery_action != "discharge"` (or `battery_kwh == 0`) for each listed hour.
- `max_grid_window`: `grid_kwh[h] <= max_grid_kwh` for each listed hour.
Also re-verify the base GridWise rules independently of the LP's own bookkeeping: energy balance holds every hour, battery bounds/rate limits hold, and end-of-day neutrality holds. This is the same check Section 08/11.2 of the Problem Statement says the judge performs independently — catching a mismatch here, before responding, is strictly better than discovering it from the hidden test results. If a replay check ever fails, this indicates an optimizer bug (the directive was validated but not correctly encoded as an LP constraint) — log it loudly; do not silently return the invalid plan.

## Deployment / submission rules that affect how this repo must be run
- Judge needs a public base URL reachable with no auth/VPN/login for both endpoints, reachable for the whole 4-hour window.
- A pullable Docker fallback image is required: exact tag/digest, exposes the documented port, binds `0.0.0.0`, no secrets baked in, reaches `/health` when run with the documented `docker run` command.
- Repo: create AFTER question reveal, keep PRIVATE during the event, make PUBLIC after the submission deadline. Never commit `.env`, API keys, or secrets.
- A 3-minute solution video is also a required deliverable (max 3:00), covering: the problem, the LLM -> guardrails -> optimizer architecture, key implementation choices, and how to run/test the submission. It carries no base points but is used as a tie-breaker — this is not something the agent can produce, so it must be flagged clearly as outstanding human work in the final report.
- README must be self-contained: clean local quickstart from a fresh clone, env var names (not values), model/provider used, LLM's role, guardrail description, optimizer/solver used, exact run command, `/health` and `/optimize-energy` curl examples, a public-sample-case test command, dependencies, known limitations, and credit for any AI coding assistant and external libraries/frameworks/SDKs used (core architecture and logic must still be the team's own work).

## Scoring priority (what to get right first if time-constrained)
1. Exact API & JSON contract (field names/types must match exactly)
2. LLM operator-note interpretation (incl. paraphrase robustness)
3. Deterministic guardrails
4. Directive application correctness in the actual schedule (extracting a directive right but not applying it still scores zero for that case)
5. Optimization cost quality
6. Reliability / deployment / Docker fallback
7. Documentation / local reproducibility
8. 3-minute video (tie-break only, no base points)
