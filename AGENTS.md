# Agent Instructions

Before editing any code in this repo, read `docs/CONTEXT.md` in full. It is the canonical spec — exact field names, directive types, and constraints. Do not guess or paraphrase schema fields from memory.

## Non-negotiables (violating any of these breaks scoring or judging)
- Endpoint names/paths are exact: `GET /health`, `POST /optimize-energy`. Do not rename, version, or prefix them.
- The LLM call is mandatory and must produce the actual `directive_interpretation` — never fake it with regex/keyword matching as the primary path, and never use the LLM only for `plan_summary`.
- Only six directive types exist: `solar_reduction`, `minimum_battery_reserve`, `no_charge_window`, `no_discharge_window`, `max_grid_window`, `no_op`. Never invent a seventh.
- `applies` is `false` only for `no_op`; every other directive type requires `applies: true`.
- `hours` arrays: unique ints 0-23, ascending, half-open interval (1PM-3PM = [13,14]).
- `minimum_battery_reserve` values must never exceed `battery.capacity_kwh` for the scenario — reject/fallback otherwise.
- End-of-day battery neutrality (`battery_energy_after_kwh` at hour 23 == `initial_energy_kwh`) is a hard LP constraint, not a soft preference.
- LLM output must pass Pydantic validation (the guardrail) before it can influence the optimizer. Malformed LLM output must degrade safely (never crash the service, never invent a directive).
- After optimizing, replay the plan against every `applies: true` directive and the base GridWise rules before responding (see "Final replay / sanity check" in `docs/CONTEXT.md`). Never return a plan that fails its own replay check.
- Never commit secrets. `OPENROUTER_API_KEY` comes from environment only, referenced in `.env.example` by name, never by value.
- `total_grid_kwh` / `total_cost_bdt` / `peak_grid_kwh` must be arithmetically derivable from `hourly_plan` — compute them from the plan, don't compute them separately and risk drift.
- The 3-minute solution video is a required deliverable but cannot be produced by an agent — always surface it as outstanding work in any final report.

Full details: `docs/CONTEXT.md`.
