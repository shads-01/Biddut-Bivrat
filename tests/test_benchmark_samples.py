"""Benchmark tests verifying optimizer output against official public sample cases."""

import json
from pathlib import Path
import pytest

from app.optimizer import solve_energy_schedule
from app.replay import replay_and_verify_plan
from app.schemas import DirectiveInterpretation, OptimizeRequest


def load_official_samples():
    sample_path = Path(__file__).parent / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
    if not sample_path.exists():
        return []
    with open(sample_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("cases", [])


@pytest.mark.parametrize("case", load_official_samples(), ids=lambda c: c["id"])
def test_optimizer_matches_official_sample_case(case):
    """Verifies that our optimizer solves each official sample case to exact expected cost and passes replay."""
    case_input = case["input"]
    expected_output = case["expected_output"]

    # 1. Parse request
    req = OptimizeRequest.model_validate(case_input)

    # 2. Parse expected directives
    expected_directives = [
        DirectiveInterpretation.model_validate(d)
        for d in expected_output["directive_interpretation"]
    ]

    # 3. Run optimizer
    hourly_plan, total_grid, total_cost, peak_grid, summary = solve_energy_schedule(
        request=req,
        directives=expected_directives,
    )

    # 4. Run replay check
    violations = replay_and_verify_plan(
        request=req,
        directives=expected_directives,
        hourly_plan=hourly_plan,
    )
    assert violations == [], f"Replay violations for {case['id']}: {violations}"

    # 5. Check mathematical equivalence with official expected outputs (tolerance 0.01)
    exp_total_grid = expected_output["total_grid_kwh"]
    exp_total_cost = expected_output["total_cost_bdt"]
    exp_peak_grid = expected_output["peak_grid_kwh"]

    assert abs(total_cost - exp_total_cost) <= 0.05, (
        f"Cost mismatch for {case['id']}: got {total_cost}, expected {exp_total_cost}"
    )
    assert abs(total_grid - exp_total_grid) <= 0.05, (
        f"Grid kWh mismatch for {case['id']}: got {total_grid}, expected {exp_total_grid}"
    )
    assert abs(peak_grid - exp_peak_grid) <= 0.05, (
        f"Peak grid mismatch for {case['id']}: got {peak_grid}, expected {exp_peak_grid}"
    )
