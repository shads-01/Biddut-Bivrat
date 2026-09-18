# GridWise — 3-Hour Team Plan (Design Spec)

Date: 2026-09-18 · BUP CSE Fest 2026 · Preliminary round, ~3 hours remaining

## Situation
- This repo IS the submission. Pipeline already works: `app/schemas.py`, `app/llm.py`,
  `app/guardrails.py`, `app/optimizer.py`, `app/replay.py`, `app/main.py`.
- 11 tests pass, 1 skipped (LLM test needs a key).
- No LLM key existed at plan time → team must create Groq + OpenRouter keys immediately.
- Everything must ship inside the remaining window. Freeze at T-15 min.

## Locked decisions
- **LLM:** Groq `llama-3.3-70b-versatile` primary; OpenRouter `meta-llama/llama-3.3-70b-instruct:free`
  fallback. Both free-tier, both OpenAI-compatible — switching = base_url + key env vars only.
  Keys from env only: `GROQ_API_KEY`, `OPENROUTER_API_KEY`. Never commit values.
- **Split:** Brain (A) / Calculator (B) / Judge (C) / Platform (D). Disjoint file ownership.
- **Ambition:** robustness maxing (fallback chain, auto-relaxation, tie-break penalties,
  caching). No demo UI, no eval dashboard — out of scope for the window.

## Frozen contract (nobody edits)
- `app/schemas.py` — request/response Pydantic models.
- `docs/CONTEXT.md` — directive shapes, endpoint names, all rules.
- Six directive types only: `solar_reduction`, `minimum_battery_reserve`, `no_charge_window`,
  `no_discharge_window`, `max_grid_window`, `no_op`.
- Hours: half-open windows, unique ints 0-23 ascending. `applies=false` only for `no_op`.
- End-of-day neutrality: `battery_energy_after_kwh[23] == initial_energy_kwh` (hard).
- Totals computed FROM `hourly_plan`, never stored separately.
- `main.py` owned solely by D.

## File ownership map
| Owner | Files |
|---|---|
| A (Brain) | `app/llm.py`, prompt string inside it, `app/fallback_parser.py` (new, optional) |
| B (Calculator) | `app/optimizer.py` |
| C (Judge) | `app/guardrails.py`, `app/replay.py`, `tests/` (incl. shared `tests/testbank.json`) |
| D (Platform) | `app/main.py`, `fly.toml`, `Dockerfile`, `.github/`, `README.md`, `.env.example` |

## Clock (from T-3:00)
| Time | Milestone |
|---|---|
| 0:00–0:20 | A: Groq key live. D: deploy hello-world. B: fuzz harness running. C: audit vs CONTEXT.md |
| 0:20–1:30 | Parallel feature build |
| 1:30–2:15 | D integrates A's llm.py → full pipeline live on deployed URL |
| 2:15–2:45 | C runs full suite against live URL; B fixes fuzz failures; A tunes prompt |
| 2:45–3:00 | FREEZE. README final, submit |

## Human-only outstanding
- 3-minute solution video (required, tie-breaker). D writes script; a human records it.
