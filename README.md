# Biddut-Bivrat: GridWise Energy Optimization Service

[![CI Test Suite](https://github.com/shads-01/Biddut-Bivrat/actions/workflows/test.yml/badge.svg)](https://github.com/shads-01/Biddut-Bivrat/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Submission for the BUP CSE Fest 2026 Hackathon preliminary (LLM-assisted campus energy scheduling).

The service receives a 24-hour campus energy scenario (demand, solar, tariff, battery) plus 1-3 natural-language operator notes. A language model reads each note and turns it into a structured directive. Deterministic guardrails validate that output, a linear program builds the cheapest valid 24-hour schedule, and a replay check re-verifies the schedule against every rule before the response is sent.

- **Live endpoint:** `https://biddut-bivrat.vercel.app` (no login or VPN needed)
- **Endpoints:** `GET /health` and `POST /optimize-energy`
- **Solver:** PuLP with the COIN-OR CBC solver

---

## How it works

```
Operator notes ──> LLM (Groq, then OpenRouter) ──> window / percent / value primitives
                                                      │
                                                      ▼
                       code converts primitives to hours, factor and kWh
                                                      │
                                                      ▼
                        Deterministic guardrails (Pydantic + checks)
                                                      │  validated directives
                                                      ▼
                            PuLP / CBC linear program (24 hours)
                                                      │  hourly plan
                                                      ▼
                     Replay check: every directive + every energy rule
                                                      │
                                                      ▼
                                                  Response
```

### 1. LLM interpretation (`app/llm.py`)
The language model is the interpreter for every operator note. It is asked for primitives only (a start and end hour, a percent, a number and unit), never for final numbers. Plain code then converts those into the exact `hours`, `factor` and `minimum_energy_kwh` values, so the model cannot produce a malformed schedule constraint on its own.

| | Provider | Model (default) | Environment variables |
|---|---|---|---|
| Primary | Groq | `qwen/qwen3.8-27b` | `GROQ_API_KEY` or `GROQ_API_KEY_1..N`, `GROQ_MODEL` |
| Backup | OpenRouter | `meta-llama/llama-3.3-70b-instruct:free` | `OPENROUTER_API_KEY` or `OPENROUTER_API_KEY_1..N`, `OPENROUTER_MODEL` |

- Several keys per provider are supported. A key that hits a rate limit (HTTP 429) is put on a 60 second cooldown and the next key is used.
- Each call uses temperature 0 and JSON output. Per-call timeouts are 8 s (Groq) and 12 s (OpenRouter).
- Results from real model answers are cached in memory by note text and battery capacity.
- **Safety net only:** if no provider answers (no key, rate limits, outage, unparseable output), a small regex parser (`app/fallback_parser.py`) covers the note so the service stays up. It is not the primary interpreter, it is less accurate on unusual wording, and its answers are never cached.

### 2. Guardrails (`app/guardrails.py`, `app/schemas.py`)
The model output is treated as untrusted. Before it reaches the optimizer:
- The directive type must be one of the six supported types. There is no way to add a seventh.
- Every note gets exactly one entry, in `note_index` order `0..N-1`. Missing, duplicate or out-of-range entries are fixed (the first entry for an index wins).
- `hours` must be unique integers from 0 to 23 in ascending order. A window such as "1 PM to 3 PM" is `[13, 14]` (start included, end excluded).
- `factor` must be within `[0, 1]`. A reserve may not exceed `battery.capacity_kwh` (it is rejected, not clamped). A grid cap must be finite and non-negative.
- `applies` is `false` only for `no_op`, whose `structured_adjustment` is `null`.
- **No invented values.** If the model picks a directive but gives no usable number (missing percent, missing grid cap, NaN), the note becomes `no_op` instead of guessing.
- Any entry that fails a check becomes a safe `no_op`. One bad note never fails the whole request.

### 3. Optimizer (`app/optimizer.py`)
A linear program over the 24 hours minimizes `sum(grid_kwh[h] * tariff[h])` subject to:
- hourly energy balance: `grid + solar_used + discharge = demand + charge`
- battery bounds (`minimum_energy_kwh` or the higher directive reserve, up to `capacity_kwh`) and hourly charge and discharge limits
- `solar_used <= solar * factor` in reduced hours (overlapping reductions multiply)
- no charging or no discharging in the listed windows, and the grid cap in the listed windows
- **end-of-day neutrality:** the battery finishes at `initial_energy_kwh`. This is a hard constraint and is never relaxed.

If the directives together make the schedule impossible, the smallest set of conflicting directives is downgraded to `no_op` (with an explanation) so the service still returns a valid plan.

### 4. Replay check (`app/replay.py`)
After solving, an independent pass re-checks every hour: energy balance, battery transitions and limits, solar usage, end-of-day neutrality, and every applied directive. `total_grid_kwh`, `total_cost_bdt` and `peak_grid_kwh` are computed from `hourly_plan` alone. A plan that fails replay is never returned (the service answers with a controlled 500 instead).

### Supported directives

| `directive_type` | Meaning | `structured_adjustment` |
|---|---|---|
| `solar_reduction` | Usable solar drops in the listed hours | `{"hours": [...], "factor": 0.2}` (fraction that remains; an 80% reduction is 0.2) |
| `minimum_battery_reserve` | Battery stays at or above a level | `{"hours": [...], "minimum_energy_kwh": 120}` |
| `no_charge_window` | Battery cannot charge | `{"hours": [...]}` |
| `no_discharge_window` | Battery cannot discharge | `{"hours": [...]}` |
| `max_grid_window` | Grid import capped per hour | `{"hours": [...], "max_grid_kwh": 155}` |
| `no_op` | Note has no effect on the schedule | `null` (with `applies: false`) |

---

## Local quickstart

Requires Python 3.11 or newer and Git.

```bash
# 1. Clone
git clone https://github.com/shads-01/Biddut-Bivrat.git
cd Biddut-Bivrat

# 2. Virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .\.venv\Scripts\Activate.ps1

# 3. Dependencies
pip install -r requirements.txt

# 4. Configuration (copy, then add your key(s))
cp .env.example .env               # Windows PowerShell: Copy-Item .env.example .env

# 5. Run
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### Environment variables (names only, never commit values)

| Variable | Purpose |
|---|---|
| `GROQ_API_KEY`, `GROQ_API_KEY_1..N` | Primary LLM provider key(s). Comma-separated values also work. |
| `GROQ_MODEL` | Groq model id (default `qwen/qwen3.8-27b`) |
| `OPENROUTER_API_KEY`, `OPENROUTER_API_KEY_1..N` | Backup LLM provider key(s) |
| `OPENROUTER_MODEL` | OpenRouter model id (default `meta-llama/llama-3.3-70b-instruct:free`) |
| `PORT` | Port used by the Docker image (default `8080`) |

Set at least one provider key. With no key the service still starts and works, using the regex fallback parser, but it will misread unusual wording.

### Check it works

```bash
curl http://localhost:8080/health
# {"status":"ok"}
```

### Run a public sample

`examples/sample-01-request.json` is the official SAMPLE-01 input. Send it:

```bash
curl -X POST http://localhost:8080/optimize-energy \
  -H "Content-Type: application/json" \
  --data @examples/sample-01-request.json
```

On Windows PowerShell use `curl.exe` instead of `curl`. To use the live deployment, replace `http://localhost:8080` with `https://biddut-bivrat.vercel.app`.

**Expected result** (full response in `examples/sample-01-response.json`; wording of `explanation` may differ, and any equally cheap schedule is valid):

| Field | Value |
|---|---|
| `directive_interpretation[0]` | `solar_reduction`, `hours` `[12, 13]`, `factor` `0.25`, `applies` `true` |
| `directive_interpretation[1]` | `no_op`, `applies` `false`, `structured_adjustment` `null` |
| `total_cost_bdt` | `38365` |
| `total_grid_kwh` | `2692.5` |
| `peak_grid_kwh` | `175` |
| `hourly_plan` | 24 entries; solar used at hours 12 and 13 is at most 45 and 42.5 kWh; battery ends at 110 kWh |

The full official pack of 10 samples, with reference costs, is in `BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json`.

---

## API

### `GET /health`
Returns `200` with `{"status": "ok"}`.

### `POST /optimize-energy`
Request body (fields other than these are ignored):

```json
{
  "scenario_id": "GRID-101",
  "operator_notes": ["Solar output will drop to about 20% from 1 PM to 3 PM."],
  "hours": [{"hour": 0, "demand_kwh": 180, "solar_kwh": 0, "tariff_bdt_per_kwh": 7}, "... 24 entries, hours 0-23 ..."],
  "battery": {
    "capacity_kwh": 500, "initial_energy_kwh": 200, "minimum_energy_kwh": 50,
    "max_charge_kwh_per_hour": 100, "max_discharge_kwh_per_hour": 100
  }
}
```

Response body: `scenario_id`, `directive_interpretation` (one entry per note), `hourly_plan` (24 entries with `grid_kwh`, `solar_used_kwh`, `battery_action`, `battery_kwh`, `battery_energy_after_kwh`), `total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`, `plan_summary`.

| Status | Meaning |
|---|---|
| 200 | Success |
| 400 | Malformed JSON or a request that breaks the schema (for example 23 hours, 4 notes, a blank note, a negative value) |
| 500 | Controlled internal error. The body is a short message with no stack trace or secrets. |

---

## Testing

```bash
pytest -q                                        # full suite (no key needed; if a key is configured, a few tests use the live model)
pytest tests/test_benchmark_samples.py -v        # the 10 official samples: costs must match the reference
python -m app.optimizer 300 42                   # 300 random scenarios through the optimizer + replay
```

The suite covers the API contract, every directive type, guardrail failure modes, replay checks, request validation, fallback parsing and 200 seeded random scenarios. It passes without any API key. If a key is configured in `.env`, a few tests call the live model.

To measure how well the live model reads notes, run the interpretation test bank with a key set:
```bash
python tests/testbank_test.py                    # against the local pipeline
DEPLOYED_URL=https://biddut-bivrat.vercel.app python tests/testbank_test.py
```

To try your own note against the live service (or `--local`) without building a request, run `python try_note.py "Do not charge the battery between 2 PM and 4 PM."`; it reuses the SAMPLE-01 scenario and prints how each note was read.

A Postman collection with the 10 official samples, reworded notes and bad-input cases (each with automated checks) is in `postman/GridWise.postman_collection.json`. Set its `baseUrl` variable to a local or deployed address. If you run it as a batch, add a 10 second delay between requests to stay inside free-tier LLM rate limits.

---

## Docker fallback image

```bash
docker pull ghcr.io/shads-01/biddut-bivrat:latest

# Pinned tag for this submission (a commit SHA):
# docker pull ghcr.io/shads-01/biddut-bivrat:<COMMIT_SHA>

docker run -d -p 8080:8080 \
  -e GROQ_API_KEY="your-groq-key" \
  ghcr.io/shads-01/biddut-bivrat:latest

curl http://localhost:8080/health
```

The image listens on `0.0.0.0:8080`, contains no secrets, and starts without any key (fallback parser only). Pass `GROQ_API_KEY` and or `OPENROUTER_API_KEY` at run time for full accuracy.

The same code also runs as a Vercel serverless function (`api/index.py`, 30 second limit) and has a `fly.toml` for Fly.io.

---

## Known limitations
- **LLM rate limits.** Free provider tiers limit how many requests per minute each key can make. When every key is limited, notes are read by the regex fallback, which handles common wordings (times, percentages, fractions, kWh and MWh) but not all of them, for example hours written as words with no time of day ("from one until three").
- **Impossible instructions.** If directives cannot all be met together, the fewest conflicting directives are downgraded to `no_op` and the explanation says so. End-of-day battery neutrality is never relaxed.
- **Reserve above capacity** is rejected and the note becomes `no_op`.
- **Cache is per process.** The interpretation cache lives in memory, so it resets on restart and is not shared between serverless instances.
- **Cold starts.** The first request after an idle period on a serverless host can take several seconds.
- Hourly resolution only, as the challenge specifies. No grid export (unused solar is curtailed).

---

## Credits
- **AI coding assistants:** Claude Code (Anthropic) and Google Antigravity (Gemini). Core architecture and logic are the team's own work.
- **LLM providers and models:** [Groq](https://groq.com) (Qwen), [OpenRouter](https://openrouter.ai) (Llama 3.3 70B), accessed through the [OpenAI Python SDK](https://github.com/openai/openai-python).
- **Libraries:** [FastAPI](https://fastapi.tiangolo.com/), [Uvicorn](https://www.uvicorn.org/), [Pydantic](https://docs.pydantic.dev/), [PuLP](https://coin-or.github.io/pulp/) with [COIN-OR CBC](https://github.com/coin-or/Cbc), [python-dotenv](https://github.com/theskumar/python-dotenv), [httpx](https://www.python-httpx.org/), [pytest](https://pytest.org/).
- **Tools:** Postman (API collection), GitHub Actions (CI and image publishing), GHCR (image hosting), Vercel (hosting).
