"""Tests for independent replay and sanity verification in app/replay.py, including deliberate corruption tests."""

import copy
import pytest
from app.optimizer import solve_energy_schedule
from app.replay import replay_and_verify_plan
from app.schemas import Battery, DirectiveInterpretation, HourEntry, OptimizeRequest, PlanHour


@pytest.fixture
def base_scenario_request():
    hours = []
    for h in range(24):
        solar = 60.0 if 10 <= h <= 15 else 0.0
        demand = 120.0 + (50.0 if 18 <= h <= 21 else 0.0)
        tariff = 12.0 if 18 <= h <= 21 else 6.0
        hours.append(HourEntry(hour=h, demand_kwh=demand, solar_kwh=solar, tariff_bdt_per_kwh=tariff))

    battery = Battery(
        capacity_kwh=500.0,
        initial_energy_kwh=200.0,
        minimum_energy_kwh=50.0,
        max_charge_kwh_per_hour=100.0,
        max_discharge_kwh_per_hour=100.0,
    )

    return OptimizeRequest(
        scenario_id="replay_test_scenario",
        operator_notes=["Test scenario"],
        hours=hours,
        battery=battery,
    )


def test_valid_solved_plan_passes_replay(base_scenario_request):
    """Ensure a correctly solved plan passes all replay checks with zero violations."""
    directives = [
        DirectiveInterpretation(
            note_index=0,
            applies=True,
            directive_type="solar_reduction",
            structured_adjustment={"hours": [12, 13], "factor": 0.5},
            explanation="Test reduction",
        )
    ]

    hourly_plan, total_grid, total_cost, peak_grid, _ = solve_energy_schedule(
        request=base_scenario_request,
        directives=directives,
    )

    violations = replay_and_verify_plan(
        request=base_scenario_request,
        directives=directives,
        hourly_plan=hourly_plan,
        total_grid_kwh=total_grid,
        total_cost_bdt=total_cost,
        peak_grid_kwh=peak_grid,
    )
    assert violations == []


def test_corrupt_energy_balance_caught(base_scenario_request):
    """Corrupt grid_kwh in one hour to create an energy imbalance and assert replay catches it."""
    directives = []
    hourly_plan, _, _, _, _ = solve_energy_schedule(base_scenario_request, directives)

    corrupted_plan = copy.deepcopy(hourly_plan)
    corrupted_plan[10].grid_kwh += 15.0  # Supply exceeds demand

    violations = replay_and_verify_plan(base_scenario_request, directives, corrupted_plan)
    assert any("energy balance violation" in v for v in violations)


def test_corrupt_solar_usage_exceeds_available(base_scenario_request):
    """Corrupt solar_used_kwh to exceed available solar and assert replay catches it."""
    directives = []
    hourly_plan, _, _, _, _ = solve_energy_schedule(base_scenario_request, directives)

    corrupted_plan = copy.deepcopy(hourly_plan)
    corrupted_plan[2].solar_used_kwh = 50.0  # Base solar at hour 2 is 0.0

    violations = replay_and_verify_plan(base_scenario_request, directives, corrupted_plan)
    assert any("exceeds available solar" in v for v in violations)


def test_corrupt_solar_reduction_directive_violation(base_scenario_request):
    """Corrupt solar_used_kwh to violate an active solar_reduction directive."""
    directive = DirectiveInterpretation(
        note_index=0,
        applies=True,
        directive_type="solar_reduction",
        structured_adjustment={"hours": [12, 13], "factor": 0.2},
        explanation="80% reduction",
    )
    hourly_plan, _, _, _, _ = solve_energy_schedule(base_scenario_request, [directive])

    corrupted_plan = copy.deepcopy(hourly_plan)
    # Allowed solar at hour 12 is 60 * 0.2 = 12 kWh; set to 30 kWh
    corrupted_plan[12].solar_used_kwh = 30.0

    violations = replay_and_verify_plan(base_scenario_request, [directive], corrupted_plan)
    assert any("Directive violation (solar_reduction)" in v for v in violations)


def test_corrupt_charge_rate_limit(base_scenario_request):
    """Corrupt charge rate to exceed max_charge_kwh_per_hour and assert replay catches it."""
    hourly_plan, _, _, _, _ = solve_energy_schedule(base_scenario_request, [])

    corrupted_plan = copy.deepcopy(hourly_plan)
    corrupted_plan[10].battery_action = "charge"
    corrupted_plan[10].battery_kwh = 150.0  # Exceeds max_charge of 100.0

    violations = replay_and_verify_plan(base_scenario_request, [], corrupted_plan)
    assert any("exceeds max_charge" in v for v in violations)


def test_corrupt_discharge_rate_limit(base_scenario_request):
    """Corrupt discharge rate to exceed max_discharge_kwh_per_hour and assert replay catches it."""
    hourly_plan, _, _, _, _ = solve_energy_schedule(base_scenario_request, [])

    corrupted_plan = copy.deepcopy(hourly_plan)
    corrupted_plan[19].battery_action = "discharge"
    corrupted_plan[19].battery_kwh = 150.0  # Exceeds max_discharge of 100.0

    violations = replay_and_verify_plan(base_scenario_request, [], corrupted_plan)
    assert any("exceeds max_discharge" in v for v in violations)


def test_corrupt_battery_energy_continuity(base_scenario_request):
    """Corrupt battery_energy_after_kwh so it doesn't match E[h-1] + charge - discharge."""
    hourly_plan, _, _, _, _ = solve_energy_schedule(base_scenario_request, [])

    corrupted_plan = copy.deepcopy(hourly_plan)
    corrupted_plan[5].battery_energy_after_kwh += 30.0  # Phantom energy injection

    violations = replay_and_verify_plan(base_scenario_request, [], corrupted_plan)
    assert any("battery energy continuity failed" in v for v in violations)


def test_corrupt_battery_minimum_reserve_violation(base_scenario_request):
    """Corrupt battery energy to violate an active minimum_battery_reserve directive."""
    directive = DirectiveInterpretation(
        note_index=0,
        applies=True,
        directive_type="minimum_battery_reserve",
        structured_adjustment={"hours": [18, 19, 20], "minimum_energy_kwh": 300.0},
        explanation="Reserve 300 kWh",
    )
    hourly_plan, _, _, _, _ = solve_energy_schedule(base_scenario_request, [directive])

    corrupted_plan = copy.deepcopy(hourly_plan)
    corrupted_plan[19].battery_energy_after_kwh = 200.0  # Violates 300.0 reserve

    violations = replay_and_verify_plan(base_scenario_request, [directive], corrupted_plan)
    assert any("Directive violation (minimum_battery_reserve)" in v for v in violations)


def test_corrupt_no_charge_window_violation(base_scenario_request):
    """Corrupt plan to charge during an active no_charge_window."""
    directive = DirectiveInterpretation(
        note_index=0,
        applies=True,
        directive_type="no_charge_window",
        structured_adjustment={"hours": [18, 19]},
        explanation="No charge during peak",
    )
    hourly_plan, _, _, _, _ = solve_energy_schedule(base_scenario_request, [directive])

    corrupted_plan = copy.deepcopy(hourly_plan)
    corrupted_plan[18].battery_action = "charge"
    corrupted_plan[18].battery_kwh = 20.0

    violations = replay_and_verify_plan(base_scenario_request, [directive], corrupted_plan)
    assert any("Directive violation (no_charge_window)" in v for v in violations)


def test_corrupt_no_discharge_window_violation(base_scenario_request):
    """Corrupt plan to discharge during an active no_discharge_window."""
    directive = DirectiveInterpretation(
        note_index=0,
        applies=True,
        directive_type="no_discharge_window",
        structured_adjustment={"hours": [2, 3]},
        explanation="No discharge at night",
    )
    hourly_plan, _, _, _, _ = solve_energy_schedule(base_scenario_request, [directive])

    corrupted_plan = copy.deepcopy(hourly_plan)
    corrupted_plan[2].battery_action = "discharge"
    corrupted_plan[2].battery_kwh = 30.0

    violations = replay_and_verify_plan(base_scenario_request, [directive], corrupted_plan)
    assert any("Directive violation (no_discharge_window)" in v for v in violations)


def test_corrupt_max_grid_window_violation(base_scenario_request):
    """Corrupt plan to exceed active max_grid_window cap."""
    directive = DirectiveInterpretation(
        note_index=0,
        applies=True,
        directive_type="max_grid_window",
        structured_adjustment={"hours": [19, 20], "max_grid_kwh": 120.0},
        explanation="Cap 120 kWh",
    )
    hourly_plan, _, _, _, _ = solve_energy_schedule(base_scenario_request, [directive])

    corrupted_plan = copy.deepcopy(hourly_plan)
    corrupted_plan[19].grid_kwh = 150.0  # Exceeds 120.0 cap

    violations = replay_and_verify_plan(base_scenario_request, [directive], corrupted_plan)
    assert any("Directive violation (max_grid_window)" in v for v in violations)


def test_corrupt_end_of_day_neutrality_violation(base_scenario_request):
    """Corrupt final hour battery energy to violate neutrality."""
    hourly_plan, _, _, _, _ = solve_energy_schedule(base_scenario_request, [])

    corrupted_plan = copy.deepcopy(hourly_plan)
    corrupted_plan[23].battery_energy_after_kwh = base_scenario_request.battery.initial_energy_kwh + 50.0

    violations = replay_and_verify_plan(base_scenario_request, [], corrupted_plan)
    assert any("End-of-day neutrality violated" in v for v in violations)


def test_corrupt_idle_action_with_positive_kwh(base_scenario_request):
    """Corrupt plan with idle action and positive battery_kwh."""
    hourly_plan, _, _, _, _ = solve_energy_schedule(base_scenario_request, [])

    corrupted_plan = copy.deepcopy(hourly_plan)
    corrupted_plan[0].battery_action = "idle"
    corrupted_plan[0].battery_kwh = 25.0

    violations = replay_and_verify_plan(base_scenario_request, [], corrupted_plan)
    assert any("battery_action is 'idle' but battery_kwh is" in v for v in violations)


def test_corrupt_totals_recomputation_mismatch(base_scenario_request):
    """Verify that recomputation mismatch of totals is caught."""
    hourly_plan, total_grid, total_cost, peak_grid, _ = solve_energy_schedule(base_scenario_request, [])

    violations = replay_and_verify_plan(
        base_scenario_request,
        [],
        hourly_plan,
        total_grid_kwh=total_grid + 10.0,  # Corrupted total grid
        total_cost_bdt=total_cost,
        peak_grid_kwh=peak_grid,
    )
    assert any("Reported total_grid_kwh" in v for v in violations)
