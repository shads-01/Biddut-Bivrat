# RUNBOOK — Person B: "The Calculator" (Optimization Engine)

You own: `app/optimizer.py` (and fuzz/test scripts under `tests/` only if C hasn't claimed
the filename — coordinate: C owns `tests/` naming, you own the fuzz logic or put it in
`app/optimizer.py`'s `__main__` block).
You do NOT touch: `main.py`, `llm.py`, `guardrails.py`, `replay.py`, `schemas.py`.

## Your core feature
The provably cost-minimal valid 24-hour schedule (25 pts correctness + 10 pts quality).
A plan that breaks any hard constraint scores ZERO for that hidden case — validity first,
cost second.

## Status
Optimizer already verified exact-match on SAMPLE-01/05/10. Your job is hardening:
tie-breaks, infeasibility recovery, numeric hygiene, and proof by fuzzing.

## Phase 1 (0:00–0:20)
Run existing tests, confirm green:
```bash
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m pytest tests/test_contract.py -q
```

## Phase 2 — Engine build (0:20–1:30)

### LP model (scipy `linprog(method="highs")` — no new deps; switch to PuLP only if blocked)
Variables per hour h (120 total):
- `g[h] ≥ 0` grid energy; `0 ≤ s[h] ≤ eff_solar[h]` solar used
- `0 ≤ c[h] ≤ max_charge[h]`; `0 ≤ d[h] ≤ max_discharge[h]`
- `min_E[h] ≤ E[h] ≤ capacity`

Objective:
```
min  Σ tariff[h]·g[h]  +  1e-6·Σ(c[h]+d[h])  +  1e-6·peak   (peak = variable, g[h] ≤ peak)
```
Constraints:
- Balance: `g[h] + s[h] + d[h] − c[h] = demand[h]` ∀h
- Battery: `E[h] − E[h−1] − c[h] + d[h] = 0`, `E[−1] = initial`
- **Neutrality: `E[23] = initial` — hard constraint, never optional**
- `g[h] ≤ grid_cap[h]` where a `max_grid_window` applies
- Directive floors: `min_E[h] = max(base_min, reserve)` for reserve hours

### Merge rules (multiple directives on same hours — most restrictive wins)
| Directive | Per-hour effect | Overlap rule |
|---|---|---|
| solar_reduction | `eff_solar[h] = solar[h] × factor` | **multiply** factors |
| minimum_battery_reserve | `min_E[h]` | **max** |
| no_charge_window | `max_charge[h] = 0` | union hours |
| no_discharge_window | `max_discharge[h] = 0` | union hours |
| max_grid_window | `grid_cap[h]` | **min** |

### Post-processing (fixed order — P15/P16/P17)
1. Net charge/discharge: `net = c[h] − d[h]` → one action per hour, EPS=1e-9.
2. Round `battery_kwh`, `solar_used` to 4 dp, clamp negatives to 0.
3. Recompute `E[h]` sequentially from initial using rounded values.
4. Fix neutrality: absorb residual drift into last non-idle hour's `battery_kwh` (re-check bounds).
5. Recompute `grid[h] = demand + charge − solar_used − discharge`, clamp ≥ 0.
6. Totals FROM the final plan: `sum(grid)`, `sum(grid·tariff)`, `max(grid)`. Never from solver internals.

### Infeasibility auto-relaxation
If solver reports infeasible (should only happen from a misread directive):
1. Don't crash. Re-solve with the regex-parser's alternative reading of each directive
   (coordinate with A — A's fallback parser exposes its reading).
2. Still infeasible → return controlled 422 (coordinate with D for the handler).
3. Log every infeasible case — it's almost always an interpretation bug, not a real scenario.

## Phase 2:15–2:45
Fix whatever C's fuzz harness finds. Fuzz loop (yours to run):
- Random feasible scenarios (solar/demand/tariff randoms, battery randoms with
  `minimum ≤ initial ≤ capacity`) × random valid directives → assert replay passes.
- Target 200+ cases. Any failure = your bug until proven otherwise.

## Done when
- [ ] All 10 public samples exact-match reference costs
- [ ] 200+ fuzz cases: zero replay failures
- [ ] Charge+discharge same hour never appears in output (check with a grep over fuzz outputs)
- [ ] Totals in response == recomputation from `hourly_plan` (assert in fuzz loop)

## Pitfalls
- Don't use a binary variable for charge/discharge exclusivity — netting after the LP is
  simpler and provably equivalent here (no efficiency losses).
- Don't compute totals from LP objective value — always from the rounded plan.
- Never let rounding break neutrality: fix it in step 4, in order.
