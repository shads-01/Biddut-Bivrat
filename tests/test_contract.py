"""Contract and optimization verification tests for GridWise service."""

import json
import os
from pathlib import Path
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient

from app.guardrails import validate_and_guard_interpretations
from app.main import app
from app.optimizer import solve_energy_schedule
from app.replay import replay_and_verify_plan
from app.schemas import Battery, DirectiveInterpretation, HourEntry, OptimizeRequest, OptimizeResponse

client = TestClient(app)


def generate_sample_scenario(scenario_id: str = "sample_scenario_01") -> dict:
    """Generates a standard 24-hour sample scenario payload."""
    hours = []
    # Demand profile: lower at night, peak in evening (18-21)
    # Solar profile: bell curve between 6 and 18
    # Tariff profile: off-peak at night, peak in evening
    for h in range(24):
        solar = 50.0 * max(0.0, 1.0 - abs(h - 12) / 6.0) if 6 <= h <= 18 else 0.0
        demand = 120.0 + (60.0 if 17 <= h <= 22 else 0.0)
        tariff = 12.0 if 17 <= h <= 22 else 6.0
        hours.append(
            {
                "hour": h,
                "demand_kwh": round(demand, 2),
                "solar_kwh": round(solar, 2),
                "tariff_bdt_per_kwh": round(tariff, 2),
            }
        )

    return {
        "scenario_id": scenario_id,
        "operator_notes": ["Reduce solar generation from 1 PM to 3 PM due to haze"],
        "hours": hours,
        "battery": {
            "capacity_kwh": 500.0,
            "initial_energy_kwh": 200.0,
            "minimum_energy_kwh": 50.0,
            "max_charge_kwh_per_hour": 100.0,
            "max_discharge_kwh_per_hour": 100.0,
        },
    }


def test_schema_valid_response_and_plan_length():
    """Verify that a valid request produces a schema-valid response with 24 hours."""
    payload = generate_sample_scenario("test_valid_schema")
    response = client.post("/optimize-energy", json=payload)
    assert response.status_code == 200
    data = response.json()

    # Validate with Pydantic model
    validated = OptimizeResponse.model_validate(data)
    assert validated.scenario_id == "test_valid_schema"
    assert len(validated.hourly_plan) == 24
    assert [p.hour for p in validated.hourly_plan] == list(range(24))


def test_totals_match_manual_recompute_from_hourly_plan():
    """Verify total_grid_kwh, total_cost_bdt, peak_grid_kwh match exact recomputation from hourly_plan."""
    payload = generate_sample_scenario("test_recompute")
    response = client.post("/optimize-energy", json=payload)
    assert response.status_code == 200
    data = response.json()

    hourly_plan = data["hourly_plan"]
    hours_map = {h["hour"]: h for h in payload["hours"]}

    recomputed_grid = round(sum(p["grid_kwh"] for p in hourly_plan), 4)
    recomputed_cost = round(
        sum(p["grid_kwh"] * hours_map[p["hour"]]["tariff_bdt_per_kwh"] for p in hourly_plan), 4
    )
    recomputed_peak = round(max(p["grid_kwh"] for p in hourly_plan), 4)

    assert abs(data["total_grid_kwh"] - recomputed_grid) < 0.01
    assert abs(data["total_cost_bdt"] - recomputed_cost) < 0.01
    assert abs(data["peak_grid_kwh"] - recomputed_peak) < 0.01


@pytest.mark.parametrize(
    "directive_type,adjustment",
    [
        ("solar_reduction", {"hours": [12, 13, 14], "factor": 0.3}),
        ("minimum_battery_reserve", {"hours": [18, 19, 20], "minimum_energy_kwh": 300.0}),
        ("no_charge_window", {"hours": [17, 18, 19, 20, 21]}),
        ("no_discharge_window", {"hours": [1, 2, 3, 4]}),
        ("max_grid_window", {"hours": [18, 19], "max_grid_kwh": 100.0}),
        ("no_op", None),
    ],
)
def test_all_directive_types_pass_replay_verification(directive_type, adjustment):
    """Test that every supported directive type is correctly solved and passes replay check."""
    payload = generate_sample_scenario(f"test_{directive_type}")
    request_obj = OptimizeRequest.model_validate(payload)

    directive = DirectiveInterpretation(
        note_index=0,
        applies=(directive_type != "no_op"),
        directive_type=directive_type,
        structured_adjustment=adjustment,
        explanation=f"Testing {directive_type}",
    )

    hourly_plan, total_grid, total_cost, peak_grid, summary = solve_energy_schedule(
        request=request_obj,
        directives=[directive],
    )

    violations = replay_and_verify_plan(
        request=request_obj,
        directives=[directive],
        hourly_plan=hourly_plan,
    )
    assert violations == [], f"Violations found for {directive_type}: {violations}"


def test_guardrail_safe_fallback_on_malformed_llm():
    """Test that malformed/corrupted LLM outputs safely fall back to no_op without failing."""
    battery = Battery(
        capacity_kwh=500.0,
        initial_energy_kwh=200.0,
        minimum_energy_kwh=50.0,
        max_charge_kwh_per_hour=100.0,
        max_discharge_kwh_per_hour=100.0,
    )
    notes = ["Invalid note 1", "Another note 2"]

    # Test 1: Completely invalid JSON
    res1 = validate_and_guard_interpretations("Not a valid json {{{", notes, battery)
    assert len(res1) == 2
    assert all(d.directive_type == "no_op" and not d.applies for d in res1)

    # Test 2: Reserve exceeding capacity
    excessive_json = json.dumps(
        {
            "interpretations": [
                {
                    "note_index": 0,
                    "applies": True,
                    "directive_type": "minimum_battery_reserve",
                    "structured_adjustment": {"hours": [10, 11], "minimum_energy_kwh": 9999.0},
                    "explanation": "Impossible reserve",
                }
            ]
        }
    )
    res2 = validate_and_guard_interpretations(excessive_json, [notes[0]], battery)
    assert len(res2) == 1
    assert res2[0].directive_type == "no_op"
    assert not res2[0].applies


def test_malformed_json_returns_400():
    """Verify that unparseable raw JSON returns HTTP 400."""
    response = client.post(
        "/optimize-energy",
        content="This is not JSON",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_public_sample_cases():
    """Runs all cases from BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json or public_sample_cases.json."""
    official_file = Path(__file__).parent / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
    fallback_file = Path(__file__).parent / "public_sample_cases.json"
    cases_file = official_file if official_file.exists() else fallback_file

    if not cases_file.exists():
        pytest.skip("No sample cases file exists")

    try:
        with open(cases_file, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
    except Exception:
        pytest.skip(f"Could not read {cases_file.name}")

    if isinstance(raw_data, dict) and "cases" in raw_data:
        case_items = [c.get("input", c) for c in raw_data["cases"]]
    elif isinstance(raw_data, list):
        case_items = raw_data
    else:
        case_items = []

    if not case_items:
        pytest.skip(f"{cases_file.name} is currently empty")

    for case_idx, case_data in enumerate(case_items):
        response = client.post("/optimize-energy", json=case_data)
        assert response.status_code == 200, f"Case {case_idx} failed: {response.text}"
        data = response.json()
        validated = OptimizeResponse.model_validate(data)
        assert len(validated.hourly_plan) == 24
        assert validated.scenario_id == case_data["scenario_id"]


def test_endpoint_passes_battery_capacity_to_llm():
    """Percent-of-capacity reserves need the scenario's real capacity, not a default."""
    payload = generate_sample_scenario("capacity_passthrough")
    payload["battery"]["capacity_kwh"] = 200.0
    with patch("app.main.call_llm_for_interpretations", return_value=None) as mock_llm:
        response = client.post("/optimize-energy", json=payload)
    assert response.status_code == 200
    mock_llm.assert_called_once_with(payload["operator_notes"], 200.0)


def test_percent_reserve_resolves_against_capacity_and_cache_is_per_capacity():
    """50% of a 200 kWh battery is 100 kWh; the same notes on a 400 kWh battery is 200 kWh."""
    import app.llm as llm

    note = [
        "Keep at least 50% of the battery capacity stored in the battery "
        "from 6 PM until 9 PM for emergency operations."
    ]
    llm._INTERPRETATION_CACHE.clear()
    with patch.object(llm, "_try_call_provider", return_value=None):
        small = json.loads(llm.call_llm_for_interpretations(note, 200.0))
        large = json.loads(llm.call_llm_for_interpretations(note, 400.0))
    llm._INTERPRETATION_CACHE.clear()

    assert small["interpretations"][0]["structured_adjustment"]["minimum_energy_kwh"] == 100.0
    assert large["interpretations"][0]["structured_adjustment"]["minimum_energy_kwh"] == 200.0
