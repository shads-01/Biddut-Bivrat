# Biddut-Bivrat — GridWise Energy Optimization Service

[![CI Test Suite](https://github.com/shads-01/Biddut-Bivrat/actions/workflows/test.yml/badge.svg)](https://github.com/shads-01/Biddut-Bivrat)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A high-reliability, containerized FastAPI service built for the **GridWise Energy Optimization Challenge**. The service ingests 24-hour microgrid scenarios alongside natural-language operator notes, translates them into structured operational directives, strictly validates constraints using deterministic guardrails, solves a cost-minimizing Linear Program (LP) via PuLP/CBC, and independently verifies all physical and directive rules through a replay engine before responding.

---

## Architecture Pipeline

```
Operator Notes (NL) ──> [Untrusted LLM: OpenRouter] ──> Structured JSON
                                                              │
                                                              ▼
Scenario & Battery ────> [Deterministic Pydantic Guardrails] ─┘
                                    │
                                    ▼ (Validated Directives & Bounds)
                         [PuLP / CBC Linear Program]
                                    │
                                    ▼ (24-Hour Schedule)
                         [Independent Replay Sanity Engine]
                                    │
                                    ▼ (Verified Compliant Plan)
                             Client Response
```

1. **Untrusted LLM (OpenRouter)**: Interprets natural language notes into one of six canonical directives (`solar_reduction`, `minimum_battery_reserve`, `no_charge_window`, `no_discharge_window`, `max_grid_window`, `no_op`).
2. **Deterministic Guardrails (`app/guardrails.py` & `app/schemas.py`)**:
   - Enforces unique ascending hours `0..23`.
   - Constrains `factor` to `[0.0, 1.0]`.
   - Validates that `minimum_battery_reserve` cannot exceed scenario `battery.capacity_kwh`.
   - Ensures `applies: false` only for `no_op`.
   - Safely degrades malformed, out-of-bounds, or unparseable outputs to non-disruptive `no_op` directives without crashing or interrupting service.
3. **Mathematical Optimizer (`app/optimizer.py`)**:
   - Formulates a 24-hour continuous Linear Program in PuLP.
   - Enforces strict hourly energy balance (`grid + solar + discharge == demand + charge`).
   - Respects battery capacity bounds, hourly charge/discharge rate limits, and solar curtailment rules.
   - Enforces **End-of-day battery neutrality** (`battery_energy_after_kwh[23] == initial_energy_kwh`) as a hard LP equality constraint.
   - Solves to global optimality with CBC solver in milliseconds.
4. **Replay & Sanity Check (`app/replay.py`)**:
   - Re-derives all metrics (`total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`) purely from `hourly_plan`.
   - Independently checks every applied directive and physical law against the plan. Catches any optimizer drift prior to client response.

---

## Outstanding Deliverables
> [!IMPORTANT]
> - **3-Minute Solution Video**: Required tie-breaker deliverable. See [VIDEO.md](VIDEO.md) for structure and guidelines.
> - **Repository Visibility**: This repository must remain **PRIVATE** during the hackathon evaluation window and switched to **PUBLIC** immediately following the submission deadline.

---

## Local Quickstart

### Prerequisites
- Python 3.11+
- Git

### 1. Clone & Set Up Virtual Environment
```bash
git clone https://github.com/shads-01/Biddut-Bivrat.git
cd Biddut-Bivrat

python -m venv .venv

# Windows (PowerShell):
.\.venv\Scripts\Activate.ps1

# Linux / macOS:
source .venv/bin/activate
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables
Copy `.env.example` to `.env`. Secrets are **never** committed to version control.
```bash
cp .env.example .env
```
Populate `.env` with your OpenRouter credentials:
```env
OPENROUTER_API_KEY=your-openrouter-api-key
OPENROUTER_MODEL=openai/gpt-4o-mini
PORT=8080
```

### 4. Run the Service Locally
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

---

## Testing & Verification

Run the comprehensive pytest suite covering health checks, schema contract verification, mathematical optimization, all directive types, and fail-safe guardrail behavior:
```bash
pytest -v
```

### Running Public Sample Cases
A sample case runner is provided in `tests/test_contract.py::test_public_sample_cases`. Drop official hackathon sample cases into `tests/public_sample_cases.json`:
```bash
pytest tests/test_contract.py -k test_public_sample_cases -v
```

---

## Live Deployment (Judge Access)

The service is deployed and publicly accessible 24/7 without authentication, VPN, or login:
- **Base URL**: `https://biddut-bivrat.vercel.app`
- **Health Check**: `https://biddut-bivrat.vercel.app/health`
- **Optimization Endpoint**: `https://biddut-bivrat.vercel.app/optimize-energy`

---

## API Endpoints & Usage

### 1. `GET /health`
Verifies service uptime and returns within 60s of container start.
```bash
curl -X GET https://biddut-bivrat.vercel.app/health
# Or locally: curl -X GET http://localhost:8080/health
```
**Response (200 OK):**
```json
{"status": "ok"}
```

### 2. `POST /optimize-energy`
Calculates the optimal cost-minimized schedule.

**Example Request:**
```bash
curl -X POST https://biddut-bivrat.vercel.app/optimize-energy \
  -H "Content-Type: application/json" \
  -d '{
    "scenario_id": "demo_scenario_01",
    "operator_notes": [
      "Heavy dust storm expected from 1 PM to 3 PM; expect 80% solar reduction."
    ],
    "hours": [
      {"hour": 0, "demand_kwh": 140.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 6.5},
      {"hour": 1, "demand_kwh": 130.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 6.5},
      {"hour": 2, "demand_kwh": 120.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 6.5},
      {"hour": 3, "demand_kwh": 115.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 6.5},
      {"hour": 4, "demand_kwh": 110.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 6.5},
      {"hour": 5, "demand_kwh": 120.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 6.5},
      {"hour": 6, "demand_kwh": 140.0, "solar_kwh": 10.0, "tariff_bdt_per_kwh": 6.5},
      {"hour": 7, "demand_kwh": 160.0, "solar_kwh": 30.0, "tariff_bdt_per_kwh": 6.5},
      {"hour": 8, "demand_kwh": 180.0, "solar_kwh": 60.0, "tariff_bdt_per_kwh": 7.0},
      {"hour": 9, "demand_kwh": 200.0, "solar_kwh": 90.0, "tariff_bdt_per_kwh": 7.0},
      {"hour": 10, "demand_kwh": 210.0, "solar_kwh": 120.0, "tariff_bdt_per_kwh": 7.0},
      {"hour": 11, "demand_kwh": 220.0, "solar_kwh": 140.0, "tariff_bdt_per_kwh": 7.0},
      {"hour": 12, "demand_kwh": 230.0, "solar_kwh": 150.0, "tariff_bdt_per_kwh": 7.0},
      {"hour": 13, "demand_kwh": 225.0, "solar_kwh": 140.0, "tariff_bdt_per_kwh": 7.0},
      {"hour": 14, "demand_kwh": 215.0, "solar_kwh": 120.0, "tariff_bdt_per_kwh": 7.0},
      {"hour": 15, "demand_kwh": 200.0, "solar_kwh": 80.0, "tariff_bdt_per_kwh": 7.0},
      {"hour": 16, "demand_kwh": 190.0, "solar_kwh": 50.0, "tariff_bdt_per_kwh": 7.0},
      {"hour": 17, "demand_kwh": 210.0, "solar_kwh": 20.0, "tariff_bdt_per_kwh": 12.0},
      {"hour": 18, "demand_kwh": 240.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 12.0},
      {"hour": 19, "demand_kwh": 250.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 12.0},
      {"hour": 20, "demand_kwh": 230.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 12.0},
      {"hour": 21, "demand_kwh": 200.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 12.0},
      {"hour": 22, "demand_kwh": 180.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 7.0},
      {"hour": 23, "demand_kwh": 150.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 6.5}
    ],
    "battery": {
      "capacity_kwh": 500.0,
      "initial_energy_kwh": 200.0,
      "minimum_energy_kwh": 50.0,
      "max_charge_kwh_per_hour": 100.0,
      "max_discharge_kwh_per_hour": 100.0
    }
  }'
```

---

## Docker & Fallback Deployment

### Fallback Image (GHCR)
The application is published to the GitHub Container Registry as the required container fallback:
```bash
docker pull ghcr.io/shads-01/biddut-bivrat:latest

docker run -d -p 8080:8080 \
  -e OPENROUTER_API_KEY="your-api-key" \
  -e OPENROUTER_MODEL="openai/gpt-4o-mini" \
  ghcr.io/shads-01/biddut-bivrat:latest
```

Verify the running container:
```bash
curl -X GET http://localhost:8080/health
```

---

## Known Limitations
- The optimizer operates on 1-hour discrete intervals per challenge specification; intra-hour solar variations or sub-hourly peaks are aggregated to hourly sums.
- If operator notes contradict each other (e.g. Simultaneous `no_charge_window` and `minimum_battery_reserve` exceeding initial energy during non-solar hours), the optimizer will evaluate whether a physically feasible schedule exists; if impossible, it safely signals a controlled internal error rather than generating physically unfeasible plans.

---

## Credits & Acknowledgments
- **AI Coding Assistant**: Google Antigravity (Gemini Flash) pair-programming agent.
- **Framework & Libraries**:
  - [FastAPI](https://fastapi.tiangolo.com/) & [Uvicorn](https://www.uvicorn.org/) for asynchronous web routing.
  - [Pydantic v2](https://docs.pydantic.dev/) for deterministic data modeling and schema validation.
  - [PuLP](https://coin-or.github.io/pulp/) & [COIN-OR CBC](https://github.com/coin-or/Cbc) for linear programming optimization.
  - [OpenAI Python SDK](https://github.com/openai/openai-python) for OpenRouter API integration.
