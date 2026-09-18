# GridWise Solution Video — Deliverable Notice

> [!IMPORTANT]
> **Status: OUTSTANDING HUMAN DELIVERABLE**
> A 3-minute video presentation is a required tie-breaker deliverable for the GridWise Challenge. It cannot be generated autonomously by an AI agent and must be recorded by the team before final submission.

## Required Video Specifications
- **Length**: Maximum 3:00 minutes (strict limit).
- **Format**: MP4 / WebM / YouTube unlisted link as specified by the hackathon submission form.
- **Audience**: Hackathon judges evaluating solution correctness, architecture, and engineering discipline.

## Suggested 3-Minute Script Structure
1. **0:00 - 0:40: Problem Statement & Value Proposition**
   - The challenge: optimizing a 24-hour microgrid/commercial energy schedule with solar, battery storage, and dynamic tariffs under unstructured operator instructions.
   - Core risk: LLMs are non-deterministic and can hallucinate invalid physics or illegal constraints.
2. **0:40 - 1:30: Robust Architecture: Untrusted LLM -> Deterministic Guardrails -> LP Solver -> Replay**
   - **LLM (OpenRouter / gpt-4o-mini)**: Performs natural language parsing of operator notes into one of six canonical directive types.
   - **Pydantic Guardrails**: Strictly validates schema, hour boundaries (0..23 ascending), factor limits [0,1], and verifies that `minimum_battery_reserve` does not exceed battery capacity. Malformed notes safely degrade to `no_op`.
   - **PuLP / CBC Linear Program**: Mathematically guarantees lowest grid cost, hourly energy balance, battery rate limits, solar availability, and end-of-day neutrality.
3. **1:30 - 2:20: Independent Replay Sanity Check & Demo**
   - Live curl invocation of `/health` (instant 200 OK) and `POST /optimize-energy`.
   - Explain how `app/replay.py` independently verifies that every applied directive and base physics rule holds on the generated plan before the response is returned to the client.
4. **2:20 - 3:00: Deployment, Reproducibility & Wrap-Up**
   - Containerized FastAPI service running on Fly.io and mirrored to GitHub Container Registry (GHCR).
   - Clean local test suite with full test coverage across all directive types.
