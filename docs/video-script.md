# 3-Minute Solution Video Script — Biddut-Bivrat (GridWise Challenge)

> **Required Deliverable Notice**:
> This video script provides the exact spoken dialogue, visual actions, and timing for the human presenter.
> The video carries tie-breaker scoring weight and must be under 3:00 minutes total.

---

## Timing Breakdown

| Section | Timestamp | Duration | Topic |
|---|---|---|---|
| **Scene 1** | 0:00 – 0:35 | 35s | The Problem & Real-World Friction |
| **Scene 2** | 0:35 – 1:25 | 50s | Architecture: Untrusted LLM → Guardrails → LP Solver → Replay |
| **Scene 3** | 1:25 – 2:15 | 50s | Live Demonstration & Replay Verification |
| **Scene 4** | 2:15 – 2:45 | 30s | Deployment, Resilience & Docker Fallback |
| **Scene 5** | 2:45 – 3:00 | 15s | Summary & Closing |

---

## Scene 1: The Problem & Real-World Friction (0:00 – 0:35)

**Visual**: Screen share of `README.md` and high-level architecture diagram.

**Spoken Script**:
> "In industrial microgrids and renewable commercial facilities, energy operators don't interact using mathematical equations — they leave messy, unstructured notes like: *'facilities are cleaning solar panels from noon to 2 PM, expect 75% reduction'* or *'grid transformer work caps import at 80 kWh'*.
>
> Simultaneously, the facility must balance dynamic time-of-use tariffs, solar availability, battery rate limits, state-of-charge bounds, and end-of-day neutrality.
>
> The fundamental engineering challenge is that LLMs are non-deterministic: you cannot trust an LLM to generate physical power schedules directly without risking illegal schedules, blackout conditions, or blown transformer thresholds.
>
> Our service, **Biddut-Bivrat**, solves this with a deterministic four-tier defense pipeline."

---

## Scene 2: Four-Tier Defense Architecture (0:35 – 1:25)

**Visual**: Pan through `app/llm.py`, `app/guardrails.py`, `app/optimizer.py`, and `app/replay.py`.

**Spoken Script**:
> "Our architecture separates language understanding from mathematical optimization:
>
> 1. **Untrusted Language Extraction**: In `app/llm.py`, we query fast frontier models (Groq Llama 3.3 70B with OpenRouter fallback) with a strict primitive extraction prompt. If the model fails or network drops, a deterministic regex fallback parser takes over.
>
> 2. **Deterministic Pydantic Guardrails**: In `app/guardrails.py`, raw LLM outputs are rigorously validated against the six canonical directive types. We verify half-open hour windows (0 to 23 ascending), clamp factors to `[0, 1]`, and verify that battery reserve requests never exceed physical capacity. Any corrupted note degrades safely to `no_op` without crashing.
>
> 3. **PuLP / CBC Linear Program**: In `app/optimizer.py`, valid directives are translated into LP constraints. PuLP solves the exact 24-hour cost minimization problem subject to hourly energy balance, battery rate limits, solar curtailment, and strict end-of-day battery neutrality in milliseconds.
>
> 4. **Independent Replay Sanity Engine**: In `app/replay.py`, before returning any JSON response, the system independently verifies every physical law and applied directive against the plan. If any constraint drifts, the service refuses to emit an invalid schedule."

---

## Scene 3: Live Demonstration (1:25 – 2:15)

**Visual**: Terminal with live `curl` against `https://biddut-bivrat.vercel.app`.

**Spoken Script**:
> "Let's see it live on our public production deployment:
>
> First, our health endpoint:
> `curl https://biddut-bivrat.vercel.app/health`
> Responds in under 100 milliseconds with status ok.
>
> Now let's send official sample scenario `SAMPLE-01` to `POST /optimize-energy`.
>
> Notice the operator notes: the first is an active note regarding rooftop panel washing from noon to 2 PM; the second is an administrative distractor about a sports registration deadline.
>
> Within 600 milliseconds, the response arrives:
> - Note 0 correctly extracted `solar_reduction` for hours 12 and 13 with factor 0.25.
> - Note 1 correctly flagged `no_op` with `applies: false`.
> - Total cost: exactly 38,365 BDT.
> - Peak grid: 175 kWh.
> - All 24 hours reflect optimal battery charging during low-tariff hours and discharging during peak rates, with battery neutrality fully preserved at hour 23."

---

## Scene 4: Deployment & Reliability (2:15 – 2:45)

**Visual**: Show GitHub Actions `docker-publish.yml` success and local Docker run.

**Spoken Script**:
> "For contest reliability, we deliver dual redundant infrastructure:
>
> 1. **Public Serverless Endpoint**: Running on Vercel at `https://biddut-bivrat.vercel.app`, with zero cold-sleep and sub-second execution.
>
> 2. **Pullable Docker Fallback Image**: Every commit to `main` builds and tests via GitHub Actions and publishes directly to GitHub Container Registry:
> `docker pull ghcr.io/shads-01/biddut-bivrat:latest`
>
> The container binds to `0.0.0.0:8080`, bundles all solver binaries, carries zero baked-in secrets, and runs completely self-contained."

---

## Scene 5: Summary & Wrap-Up (2:45 – 3:00)

**Visual**: Show test suite passing (22/22 tests, all 10 sample cases green).

**Spoken Script**:
> "To conclude: Biddut-Bivrat delivers 100% contract compliance, mathematical cost optimality, sub-second latency, and deterministic safety against hallucinated operational directives.
>
> All code is clean, fully tested, and ready for evaluation. Thank you."
