"""Fuzzing test harness generating 200+ random valid microgrid scenarios and directive combinations."""

import random
import pytest
from app.optimizer import solve_energy_schedule
from app.replay import replay_and_verify_plan
from app.schemas import Battery, DirectiveInterpretation, HourEntry, OptimizeRequest


def generate_random_scenario(seed: int) -> OptimizeRequest:
    rng = random.Random(seed)

    capacity = rng.uniform(300.0, 800.0)
    min_energy = rng.uniform(20.0, 60.0)
    initial_energy = rng.uniform(min_energy + 50.0, capacity * 0.75)
    max_charge = rng.uniform(80.0, 150.0)
    max_discharge = rng.uniform(80.0, 150.0)

    battery = Battery(
        capacity_kwh=round(capacity, 2),
        initial_energy_kwh=round(initial_energy, 2),
        minimum_energy_kwh=round(min_energy, 2),
        max_charge_kwh_per_hour=round(max_charge, 2),
        max_discharge_kwh_per_hour=round(max_discharge, 2),
    )

    hours = []
    for h in range(24):
        # Solar peak midday (9-16)
        if 8 <= h <= 17:
            solar = rng.uniform(20.0, 150.0) * max(0.0, 1.0 - abs(h - 12.5) / 5.5)
        else:
            solar = 0.0

        demand = rng.uniform(60.0, 180.0)
        tariff = rng.uniform(10.0, 16.0) if 17 <= h <= 22 else rng.uniform(4.0, 8.0)

        hours.append(
            HourEntry(
                hour=h,
                demand_kwh=round(demand, 2),
                solar_kwh=round(solar, 2),
                tariff_bdt_per_kwh=round(tariff, 2),
            )
        )

    return OptimizeRequest(
        scenario_id=f"fuzz_scenario_{seed}",
        operator_notes=["Fuzz test note"],
        hours=hours,
        battery=battery,
    )


def generate_random_directives(seed: int, battery: Battery) -> list[DirectiveInterpretation]:
    rng = random.Random(seed + 10000)
    num_directives = rng.randint(0, 3)
    directives = []

    types = [
        "solar_reduction",
        "minimum_battery_reserve",
        "no_charge_window",
        "no_discharge_window",
        "max_grid_window",
        "no_op",
    ]

    for idx in range(num_directives):
        dtype = rng.choice(types)
        start = rng.randint(0, 20)
        length = rng.randint(1, 4)
        end = min(24, start + length)
        hours = list(range(start, end))

        if dtype == "solar_reduction":
            factor = round(rng.uniform(0.1, 0.9), 2)
            adj = {"hours": hours, "factor": factor}
        elif dtype == "minimum_battery_reserve":
            # Feasible reserve floor
            reserve = round(rng.uniform(battery.minimum_energy_kwh, min(battery.capacity_kwh * 0.7, battery.initial_energy_kwh + 60.0)), 2)
            adj = {"hours": hours, "minimum_energy_kwh": reserve}
        elif dtype in ("no_charge_window", "no_discharge_window"):
            adj = {"hours": hours}
        elif dtype == "max_grid_window":
            # Grid cap that guarantees feasibility given max_discharge
            cap = round(rng.uniform(180.0, 300.0), 2)
            adj = {"hours": hours, "max_grid_kwh": cap}
        else:
            adj = None

        directives.append(
            DirectiveInterpretation(
                note_index=idx,
                applies=(dtype != "no_op"),
                directive_type=dtype,
                structured_adjustment=adj,
                explanation=f"Fuzz directive {dtype}",
            )
        )

    return directives


@pytest.mark.parametrize("seed", range(1, 201))
def test_fuzz_200_scenarios_pass_replay(seed: int):
    """Generates 200 random valid scenarios with randomized directives and asserts 100% replay compliance."""
    req = generate_random_scenario(seed)
    directives = generate_random_directives(seed, req.battery)

    hourly_plan, total_grid, total_cost, peak_grid, summary = solve_energy_schedule(
        request=req,
        directives=directives,
    )

    assert len(hourly_plan) == 24

    # 1. Verify charge and discharge are mutually exclusive (handled via single battery_action)
    for p in hourly_plan:
        assert p.battery_action in ("charge", "discharge", "idle")
        if p.battery_action == "idle":
            assert p.battery_kwh <= 0.01

    # 2. Verify replay validator passes with 0 violations
    violations = replay_and_verify_plan(
        request=req,
        directives=directives,
        hourly_plan=hourly_plan,
        total_grid_kwh=total_grid,
        total_cost_bdt=total_cost,
        peak_grid_kwh=peak_grid,
    )
    assert violations == [], f"Fuzz seed {seed} failed replay: {violations}"
