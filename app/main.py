"""FastAPI application for GridWise Energy Optimization service."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.guardrails import validate_and_guard_interpretations
from app.llm import call_llm_for_interpretations
from app.optimizer import solve_energy_schedule
from app.replay import replay_and_verify_plan
from app.schemas import HealthResponse, OptimizeRequest, OptimizeResponse

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("gridwise.main")

app = FastAPI(
    title="GridWise Energy Optimizer",
    version="1.0.0",
    docs_url=None,  # No dashboard/docs required by spec
    redoc_url=None,
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Ensure malformed JSON or invalid schema requests return HTTP 400 per contest spec."""
    logger.warning(f"Request validation error on {request.url.path}: {exc.errors()}")
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": "Malformed JSON or invalid request payload."},
    )


@app.exception_handler(json.JSONDecodeError)
async def json_decode_exception_handler(request: Request, exc: json.JSONDecodeError):
    logger.warning(f"JSON decode error: {exc}")
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": "Malformed JSON body."},
    )


@app.get(
    "/health",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    summary="Health check endpoint",
)
async def health():
    """Returns 200 {'status': 'ok'} within 60s of service start."""
    return HealthResponse(status="ok")


@app.post(
    "/optimize-energy",
    response_model=OptimizeResponse,
    status_code=status.HTTP_200_OK,
    summary="Optimize 24-hour energy schedule from scenario and operator notes",
)
async def optimize_energy(request: Request):
    """Processes energy scenario and operator notes to generate a verified, optimal schedule."""
    # 1. Parse raw body to guarantee 400 for malformed JSON
    try:
        raw_body = await request.body()
        if not raw_body:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"detail": "Empty request body"},
            )
        json_data = json.loads(raw_body)
    except Exception as e:
        logger.warning(f"Malformed JSON body received: {e}")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "Invalid JSON format"},
        )

    # 2. Validate against OptimizeRequest schema
    try:
        req = OptimizeRequest.model_validate(json_data)
    except Exception as e:
        logger.warning(f"Schema validation failed: {e}")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "Request body does not conform to required schema"},
        )

    # 3. Pipeline execution with safe controlled error handling (HTTP 500, no leaks)
    try:
        # Step A: LLM call to interpret operator notes (untrusted)
        raw_llm_output = call_llm_for_interpretations(
            req.operator_notes, req.battery.capacity_kwh
        )

        # Step B: Deterministic guardrails (validate & fail-safe to no_op)
        directives = validate_and_guard_interpretations(
            raw_llm_output=raw_llm_output,
            operator_notes=req.operator_notes,
            battery=req.battery,
        )

        # Step C: Optimization via PuLP linear program
        hourly_plan, total_grid, total_cost, peak_grid, summary = solve_energy_schedule(
            request=req,
            directives=directives,
        )

        # Step D: Replay & sanity check against directives and base physics
        violations = replay_and_verify_plan(
            request=req,
            directives=directives,
            hourly_plan=hourly_plan,
        )

        if violations:
            logger.critical(
                f"Replay detected {len(violations)} constraint violations in scenario {req.scenario_id}! "
                f"Violations: {violations}"
            )
            # In an actual runtime scenario where LP solver somehow produced an invalid plan,
            # fail safely with controlled 500 rather than emitting a broken plan to judges.
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"detail": "Internal optimization constraint verification failed."},
            )

        # Step E: Return verified response
        return OptimizeResponse(
            scenario_id=req.scenario_id,
            directive_interpretation=directives,
            hourly_plan=hourly_plan,
            total_grid_kwh=total_grid,
            total_cost_bdt=total_cost,
            peak_grid_kwh=peak_grid,
            plan_summary=summary,
        )

    except Exception as exc:
        logger.error("Controlled error in /optimize-energy pipeline", exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "An internal error occurred while processing the energy schedule."},
        )
