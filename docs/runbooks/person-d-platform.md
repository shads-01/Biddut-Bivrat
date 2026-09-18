# RUNBOOK — Person D: "The Platform" (API, Deployment, Docs)

You own: `app/main.py`, `fly.toml`, `Dockerfile`, `.github/`, `README.md`, `.env.example`.
You do NOT touch: `llm.py`, `optimizer.py`, `guardrails.py`, `replay.py`.
Exception: `requirements.txt` — coordinate in chat; one-line adds only.

## Your core feature
A public URL that judges can reach for 4 hours straight. **An unreachable service scores
nothing** — your feature is the one that can zero out everyone else's.

## Phase 1 — Deploy hello-world (0:00–0:30) — FIRST PRIORITY
1. `fly.toml` already exists — check it: `internal_port`, `min_machines_running = 1`
   (never sleep). If Fly needs a card, fall back to Render/Railway paid-always-on, or any
   VPS. **No free tiers that sleep.**
2. Deploy the current (already-working) pipeline immediately — don't wait for A.
3. Verify from OUTSIDE localhost:
   ```bash
   curl https://<your-app>.fly.dev/health        # expect {"status": "ok"}
   curl -X POST https://<your-app>.fly.dev/optimize-energy \
        -H "Content-Type: application/json" -d @tests/public_sample_cases.json[0]
   ```
4. Warm it up and note cold-start time. `/health` must answer within 60s of start,
   `/optimize-energy` within 30s, p95 ≤ 5s for full latency credit.

## Phase 2 — Handlers + infra (0:30–1:30)

### Error handling in `main.py`
- Override FastAPI's 422 RequestValidationError handler → return **400** for malformed JSON
  (spec wants 400 for structural errors)
- 422 for well-formed but semantically impossible input (e.g. `initial > capacity`)
- Global exception handler → controlled `{"error": "internal_error"}`, **no stack trace,
  no LLM errors, no secrets echoed**
- Load LLM client + solver at startup, not per request

### Secrets
- `.env` in `.gitignore` (verify), `.env.example` lists names only: `GROQ_API_KEY`,
  `OPENROUTER_API_KEY` — never values
- Grep the repo for any leaked key before freeze: `git grep -i "sk-\|gsk_"`

### Docker fallback (required deliverable)
- Build + push an image with an EXACT tag, `0.0.0.0` bind, documented port, no baked secrets
- Verify: `docker run -p 8080:8080 <image:tag>` → `/health` responds

### Integration (1:30–2:15)
- Merge A's `llm.py` into the deployed build; if A's engine isn't ready, deploy with the
  current fallback path — **a live imperfect baseline beats a perfect unshipped one**
- Redeploy, hand the live URL to C for the 2:15 convergence loop

## Phase 2:45–3:00 — Freeze & close
- README checklist (self-contained per CONTEXT.md):
  - [ ] Fresh-clone quickstart (works on a machine that never saw this repo)
  - [ ] Env var NAMES only, model/provider named, LLM's role + guardrail description +
        solver named
  - [ ] Exact run command; `/health` + `/optimize-energy` curl examples; public-sample
        test command; dependencies; known limitations; AI-assistant credit
- Write the 3-minute video script to `docs/video-script.md`: problem → LLM→guardrails→
  optimizer architecture → key choices → run/test demo. (A human records it — flag as
  outstanding in the final report.)
- Final grep for secrets, final `/health` check, stop. **No risky changes after 2:45.**

## Done when
- [ ] Live URL: both endpoints green from outside, warm p95 < 5s
- [ ] `docker run <image:tag>` → `/health` green
- [ ] Fresh clone + README → running in <5 min (actually test this)
- [ ] Zero secrets in repo/logs/responses
- [ ] Video script written

## Pitfalls
- Don't deploy "later" — deploy at 0:15 even if it's hello-world. Late deployment is the
  #1 hackathon killer.
- Don't let the README say "TODO" anywhere; judges grade Documentation (10 pts).
- Don't touch other people's files to "fix" something — report it to the owner instead.
