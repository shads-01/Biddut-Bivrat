# RUNBOOK — Person C: "The Judge" (Guardrails, Replay, Eval Harness)

You own: `app/guardrails.py`, `app/replay.py`, `tests/` (including the shared
`tests/testbank.json` everyone else consumes).
You do NOT touch: `llm.py`, `optimizer.py`, `main.py`.

## Your core feature
Two trust boundaries: (1) untrusted LLM output must never reach the optimizer unvalidated;
(2) our own plan must be replay-validated before it leaves the server. Plus: you are the
team's scorekeeper — your test bank tells A how good the interpretation is.

## Phase 1 (0:00–0:20) — Audit against CONTEXT.md
Read `docs/CONTEXT.md` in full. Diff every check it lists against what
`guardrails.py` and `replay.py` actually do today. Write the gap list. Fix gaps first.

## Phase 2 — Build (0:20–1:30)

### Guardrail checks (per CONTEXT.md — every one of these)
- `directive_type` ∈ six allowed values → anything else: downgrade that entry to `no_op`
- Exactly one entry per note, `note_index` 0..N−1, no gaps/dupes → fill missing with `no_op`
- `hours`: non-empty, unique ints 0–23, **ascending** → else downgrade entry
- `factor` ∈ [0,1] finite → else downgrade
- `minimum_energy_kwh`: finite, ≥ 0, **≤ battery.capacity_kwh** → else downgrade
- `max_grid_kwh`: finite, ≥ 0 → else downgrade
- `no_op` → `applies=false`, `structured_adjustment=null`; every other type → `applies=true`
  with the EXACT required keys (no extras, no missing)
- Downgrade ONE bad entry, keep the rest — never fail the whole request
- Build final `structured_adjustment` objects in code; never pass raw LLM JSON through

### Replay validator (`app/replay.py`) — mirror of the judge (spec §11.1–11.3)
- 24 unique hours 0–23; all values finite, non-negative (tolerance 0.01)
- Energy balance every hour: `grid + solar_used + discharge == demand + charge`
- Solar: `solar_used[h] ≤ base_solar[h] × factor` in solar_reduction hours
- Battery bounds (incl. directive reserve floors), rate limits, action/amount consistency
- `no_charge_window` → action ≠ charge (or kwh=0); `no_discharge_window` → same for discharge
- `max_grid_window` → `grid_kwh[h] ≤ max_grid_kwh`
- Neutrality: `battery_energy_after_kwh[23] == initial_energy_kwh`
- Totals match recomputation from `hourly_plan`
- A failed replay = log LOUDLY, never silently return the invalid plan

### Test bank: `tests/testbank.json` (you own this file — it's the team's scoreboard)
40-60 notes. Structure: `[{note, expected: {directive_type, hours, value}}]`. Must include:
- Each directive type 5-8 paraphrase ways ("PV", "rooftop solar", "panel output", "one-fifth",
  "80% reduction", "13:00–15:00", "from one until three")
- to-vs-by pairs, "halved", "no solar"
- 12h/24h/spoken times, noon, midnight, wrap-around ("10 PM to 2 AM" → [0,1,22,23])
- kWh / MWh / percent-of-capacity reserves ("half the battery")
- 10+ distractors, incl. ones mentioning solar/battery without being instructions
- 5-8 `no_op` near-misses ("tariff is high in the evening")

### Eval runner
`pytest tests/testbank_test.py` (or a small script): runs the full pipeline (or llm.py
directly) over the bank, prints **accuracy %** and every failure with expected vs got.
Re-runnable in one command — this is A's scoreboard at 2:15.

## Phase 2:15–2:45 — The convergence loop
1. Point the eval runner at the **deployed URL** (D gives it to you by 1:30).
2. Run all public samples + test bank against live. Every failure goes to the owner:
   interpretation miss → A; replay/numeric failure → B; handler/contract issue → D.
3. Re-run until green. This is the last gate before freeze.

## Done when
- [ ] Guardrail gap list from Phase 1 fully closed
- [ ] Replay validator catches a deliberately corrupted plan (test this: corrupt one value, assert it fails)
- [ ] Fuzz + public samples + test bank all green against the live URL
- [ ] Accuracy score printed, ≥90%

## Pitfalls
- Don't let a guardrail check crash on weird input (e.g. `hours: "13-14"` string) — catch,
  downgrade to `no_op`.
- Tolerance is 0.01 — don't be stricter in replay than the judge, you'll reject valid plans.
