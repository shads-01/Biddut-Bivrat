"""PuLP Linear Programming optimizer for GridWise 24-hour schedule."""

from __future__ import annotations

import logging
import random
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
import pulp

from app.schemas import (
    Battery,
    DirectiveInterpretation,
    HourEntry,
    OptimizeRequest,
    PlanHour,
)

logger = logging.getLogger("gridwise.optimizer")


def solve_energy_schedule(
    request: OptimizeRequest,
    directives: List[DirectiveInterpretation],
) -> Tuple[List[PlanHour], float, float, float, str]:
    """Builds and solves the 24-hour cost-minimizing LP schedule.

    Returns:
        hourly_plan: 24 PlanHour items
        total_grid_kwh: recalculated from hourly_plan
        total_cost_bdt: recalculated from hourly_plan
        peak_grid_kwh: recalculated from hourly_plan
        plan_summary: human-readable overview
    """
    hours_map: Dict[int, HourEntry] = {h.hour: h for h in request.hours}
    battery: Battery = request.battery

    # Prepare directive impact lookup tables
    solar_factors: Dict[int, float] = {h: 1.0 for h in range(24)}
    min_battery_reserves: Dict[int, float] = {h: battery.minimum_energy_kwh for h in range(24)}
    no_charge_hours: set[int] = set()
    no_discharge_hours: set[int] = set()
    max_grid_limits: Dict[int, float] = {}

    for d in directives:
        if not d.applies or not d.structured_adjustment:
            continue
        adj = d.structured_adjustment
        adj_hours = adj.get("hours", [])

        if d.directive_type == "solar_reduction":
            factor = float(adj.get("factor", 1.0))
            for h in adj_hours:
                if 0 <= h < 24:
                    solar_factors[h] = min(solar_factors[h], factor)

        elif d.directive_type == "minimum_battery_reserve":
            req_min = float(adj.get("minimum_energy_kwh", battery.minimum_energy_kwh))
            for h in adj_hours:
                if 0 <= h < 24:
                    min_battery_reserves[h] = max(min_battery_reserves[h], req_min)

        elif d.directive_type == "no_charge_window":
            for h in adj_hours:
                if 0 <= h < 24:
                    no_charge_hours.add(h)

        elif d.directive_type == "no_discharge_window":
            for h in adj_hours:
                if 0 <= h < 24:
                    no_discharge_hours.add(h)

        elif d.directive_type == "max_grid_window":
            limit = float(adj.get("max_grid_kwh", 0.0))
            for h in adj_hours:
                if 0 <= h < 24:
                    if h in max_grid_limits:
                        max_grid_limits[h] = min(max_grid_limits[h], limit)
                    else:
                        max_grid_limits[h] = limit

    # Initialize PuLP Problem
    prob = pulp.LpProblem(f"GridWise_Scenario_{request.scenario_id}", pulp.LpMinimize)

    # Decision variables for 24 hours
    grid_vars: Dict[int, pulp.LpVariable] = {}
    solar_vars: Dict[int, pulp.LpVariable] = {}
    charge_vars: Dict[int, pulp.LpVariable] = {}
    discharge_vars: Dict[int, pulp.LpVariable] = {}
    e_after_vars: Dict[int, pulp.LpVariable] = {}

    for h in range(24):
        # Grid import: non-negative, optional upper limit from max_grid_window
        max_grid = max_grid_limits.get(h, None)
        grid_vars[h] = pulp.LpVariable(
            f"grid_{h}",
            lowBound=0.0,
            upBound=max_grid,
            cat=pulp.LpContinuous,
        )

        # Solar used: 0 <= solar_used <= effective_solar_kwh
        base_solar = hours_map[h].solar_kwh
        effective_solar = base_solar * solar_factors[h]
        solar_vars[h] = pulp.LpVariable(
            f"solar_{h}",
            lowBound=0.0,
            upBound=effective_solar,
            cat=pulp.LpContinuous,
        )

        # Battery charging: 0 <= charge <= max_charge_kwh_per_hour (or 0 if no_charge_window)
        up_charge = 0.0 if h in no_charge_hours else battery.max_charge_kwh_per_hour
        charge_vars[h] = pulp.LpVariable(
            f"charge_{h}",
            lowBound=0.0,
            upBound=up_charge,
            cat=pulp.LpContinuous,
        )

        # Battery discharging: 0 <= discharge <= max_discharge_kwh_per_hour (or 0 if no_discharge_window)
        up_discharge = 0.0 if h in no_discharge_hours else battery.max_discharge_kwh_per_hour
        discharge_vars[h] = pulp.LpVariable(
            f"discharge_{h}",
            lowBound=0.0,
            upBound=up_discharge,
            cat=pulp.LpContinuous,
        )

        # Battery energy after: minimum_reserve <= e_after <= capacity_kwh
        min_reserve = min_battery_reserves[h]
        e_after_vars[h] = pulp.LpVariable(
            f"e_after_{h}",
            lowBound=min_reserve,
            upBound=battery.capacity_kwh,
            cat=pulp.LpContinuous,
        )

    peak_grid_var = pulp.LpVariable("peak_grid", lowBound=0.0, cat=pulp.LpContinuous)
    for h in range(24):
        prob += (peak_grid_var >= grid_vars[h], f"Peak_Grid_Bound_{h}")

    # Objective function: minimize sum(grid_kwh[h] * tariff_bdt[h]) + small tie-breakers
    # Tie-breaker (1e-6) on charge+discharge prevents simultaneous charging & discharging degeneracy
    # Tie-breaker (1e-5) on peak_grid selects the lowest peak when multiple schedules have identical cost
    prob += (
        pulp.lpSum(
            grid_vars[h] * hours_map[h].tariff_bdt_per_kwh + 1e-6 * (charge_vars[h] + discharge_vars[h])
            for h in range(24)
        )
        + 1e-5 * peak_grid_var,
        "Total_Cost_Objective",
    )

    # Constraints
    for h in range(24):
        demand = hours_map[h].demand_kwh

        # 1. Energy balance: grid + solar + discharge == demand + charge
        prob += (
            grid_vars[h] + solar_vars[h] + discharge_vars[h] == demand + charge_vars[h],
            f"Energy_Balance_{h}",
        )

        # 2. Battery state transition: E_after[h] == E_before[h] + charge[h] - discharge[h]
        if h == 0:
            prob += (
                e_after_vars[0] == battery.initial_energy_kwh + charge_vars[0] - discharge_vars[0],
                "Battery_State_0",
            )
        else:
            prob += (
                e_after_vars[h] == e_after_vars[h - 1] + charge_vars[h] - discharge_vars[h],
                f"Battery_State_{h}",
            )

    # 3. End-of-day neutrality constraint (hard requirement): E_after[23] == initial_energy_kwh
    prob += (
        e_after_vars[23] == battery.initial_energy_kwh,
        "End_Of_Day_Neutrality",
    )

    # Solve using default CBC solver silently
    solver = pulp.PULP_CBC_CMD(msg=False)
    status = prob.solve(solver)

    if status != pulp.LpStatusOptimal:
        logger.error(f"LP solve failed or infeasible. Status code: {status}, Status text: {pulp.LpStatus[status]}")
        raise ValueError(f"Optimizer could not find optimal schedule. Status: {pulp.LpStatus[status]}")

    # Build hourly_plan
    hourly_plan: List[PlanHour] = []
    TOLERANCE = 1e-5

    for h in range(24):
        raw_grid = float(pulp.value(grid_vars[h]) or 0.0)
        raw_solar = float(pulp.value(solar_vars[h]) or 0.0)
        raw_charge = float(pulp.value(charge_vars[h]) or 0.0)
        raw_discharge = float(pulp.value(discharge_vars[h]) or 0.0)
        raw_e_after = float(pulp.value(e_after_vars[h]) or 0.0)

        # Clean small negative floats from solver precision
        grid = max(0.0, raw_grid)
        solar = max(0.0, raw_solar)
        charge = max(0.0, raw_charge)
        discharge = max(0.0, raw_discharge)

        # Reconcile any simultaneous charge/discharge micro-artifacts
        net_flow = charge - discharge
        if net_flow > TOLERANCE:
            action = "charge"
            battery_kwh = net_flow
        elif net_flow < -TOLERANCE:
            action = "discharge"
            battery_kwh = -net_flow
        else:
            action = "idle"
            battery_kwh = 0.0

        hourly_plan.append(
            PlanHour(
                hour=h,
                grid_kwh=round(grid, 4),
                solar_used_kwh=round(solar, 4),
                battery_action=action,
                battery_kwh=round(battery_kwh, 4),
                battery_energy_after_kwh=round(raw_e_after, 4),
            )
        )

    # Recalculate summary metrics strictly from hourly_plan alone
    total_grid_kwh = round(sum(p.grid_kwh for p in hourly_plan), 4)
    total_cost_bdt = round(sum(p.grid_kwh * hours_map[p.hour].tariff_bdt_per_kwh for p in hourly_plan), 4)
    peak_grid_kwh = round(max(p.grid_kwh for p in hourly_plan), 4)

    plan_summary = (
        f"Optimized 24h schedule for {request.scenario_id}: Total Grid = {total_grid_kwh:.2f} kWh, "
        f"Total Cost = {total_cost_bdt:.2f} BDT, Peak Grid = {peak_grid_kwh:.2f} kWh. "
        f"All {len(directives)} directives and battery neutrality satisfied."
    )

    return hourly_plan, total_grid_kwh, total_cost_bdt, peak_grid_kwh, plan_summary


# ---------------------------------------------------------------------------
# Fuzz harness: .venv/Scripts/python -m app.optimizer [cases] [seed]
# Lives here (not tests/) because tests/ is owned by Person C.
# ---------------------------------------------------------------------------

FUZZ_TIME_BUDGET_S = 3.0  # per request, relaxation included; API limit is 30 s, p95 target 5 s


def _directive(index: int, kind: str, adjustment: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "note_index": index,
        "applies": kind != "no_op",
        "directive_type": kind,
        "structured_adjustment": adjustment,
        "explanation": "fuzz",
    }


def _build_case(
    scenario_id: str,
    hours: List[Dict[str, Any]],
    battery: Dict[str, float],
    raw_directives: List[Dict[str, Any]],
) -> Tuple[OptimizeRequest, List[DirectiveInterpretation]]:
    request = OptimizeRequest.model_validate({
        "scenario_id": scenario_id,
        "operator_notes": [f"fuzz note {i}" for i in range(len(raw_directives))],
        "hours": hours,
        "battery": battery,
    })
    return request, [DirectiveInterpretation.model_validate(d) for d in raw_directives]


def _fixed_case(raw_directives: List[Dict[str, Any]]) -> Tuple[OptimizeRequest, List[DirectiveInterpretation]]:
    """Flat 200 kWh demand, 150 kWh solar 10:00-15:00, evening peak tariff, 500 kWh battery at 200."""
    hours = [
        {
            "hour": h,
            "demand_kwh": 200.0,
            "solar_kwh": 150.0 if 10 <= h <= 14 else 0.0,
            "tariff_bdt_per_kwh": 12.0 if 17 <= h <= 22 else 7.0,
        }
        for h in range(24)
    ]
    battery = {
        "capacity_kwh": 500.0,
        "initial_energy_kwh": 200.0,
        "minimum_energy_kwh": 50.0,
        "max_charge_kwh_per_hour": 100.0,
        "max_discharge_kwh_per_hour": 100.0,
    }
    return _build_case("FIXED", hours, battery, raw_directives)


def _fuzz_case(rng: random.Random, scenario_id: str) -> Tuple[OptimizeRequest, List[DirectiveInterpretation]]:
    capacity = rng.randint(150, 800)
    minimum = rng.randint(0, capacity // 3)
    battery = {
        "capacity_kwh": float(capacity),
        "minimum_energy_kwh": float(minimum),
        "initial_energy_kwh": float(rng.randint(minimum, capacity)),
        "max_charge_kwh_per_hour": float(rng.randint(20, 200)),
        "max_discharge_kwh_per_hour": float(rng.randint(20, 200)),
    }
    solar_peak = rng.uniform(0.0, 300.0)
    hours = [
        {
            "hour": h,
            "demand_kwh": round(rng.uniform(40.0, 320.0), 2),
            "solar_kwh": round(max(0.0, solar_peak * (1 - abs(h - 12) / 6)), 2),
            "tariff_bdt_per_kwh": round(rng.choice([6.0, 7.5, 9.0, 11.0, 13.5]) + rng.uniform(-0.5, 0.5), 2),
        }
        for h in range(24)
    ]
    raw_directives = []
    for index in range(rng.randint(1, 3)):
        kind = rng.choice([
            "solar_reduction", "minimum_battery_reserve", "no_charge_window",
            "no_discharge_window", "max_grid_window", "no_op",
        ])
        if kind == "no_op":
            raw_directives.append(_directive(index, kind, None))
            continue
        start = rng.randint(0, 22)
        adjustment: Dict[str, Any] = {"hours": list(range(start, min(24, start + rng.randint(1, 4))))}
        if kind == "solar_reduction":
            adjustment["factor"] = rng.choice([0.0, 0.2, 0.25, 0.5, 0.8, 1.0])
        elif kind == "minimum_battery_reserve":
            adjustment["minimum_energy_kwh"] = float(rng.randint(minimum, capacity))
        elif kind == "max_grid_window":
            adjustment["max_grid_kwh"] = float(rng.randint(0, 400))
        raw_directives.append(_directive(index, kind, adjustment))
    return _build_case(scenario_id, hours, battery, raw_directives)


def _fuzz_run(
    request: OptimizeRequest,
    directives: List[DirectiveInterpretation],
) -> Tuple[List[str], Optional[List[PlanHour]]]:
    """Runs one request the way main.py does (solve, then replay the same list) and lists every problem."""
    from app.replay import replay_and_verify_plan  # read-only use of Person C's module

    started = time.perf_counter()
    try:
        plan, total_grid, total_cost, peak_grid, _ = solve_energy_schedule(request, directives)
    except Exception as exc:  # the harness must report, never stop
        return [f"solver raised {type(exc).__name__}: {exc}"], None
    elapsed = time.perf_counter() - started

    problems = list(replay_and_verify_plan(request, directives, plan))
    tariff = {entry.hour: entry.tariff_bdt_per_kwh for entry in request.hours}
    if [p.hour for p in plan] != list(range(24)):
        problems.append("hourly_plan hours are not exactly 0..23 in order")
    if abs(total_grid - sum(p.grid_kwh for p in plan)) > 0.01:
        problems.append(f"total_grid_kwh {total_grid} != sum of plan")
    if abs(total_cost - sum(p.grid_kwh * tariff[p.hour] for p in plan)) > 0.01:
        problems.append(f"total_cost_bdt {total_cost} != sum of plan")
    if abs(peak_grid - max(p.grid_kwh for p in plan)) > 0.01:
        problems.append(f"peak_grid_kwh {peak_grid} != max of plan")
    for p in plan:
        if (p.battery_action == "idle") != (p.battery_kwh == 0.0):
            problems.append(f"hour {p.hour}: action {p.battery_action} with battery_kwh {p.battery_kwh}")
    if elapsed > FUZZ_TIME_BUDGET_S:
        problems.append(f"solve took {elapsed:.2f}s (> {FUZZ_TIME_BUDGET_S}s)")
    return problems, plan


def _fuzz_fixed_checks() -> List[str]:
    failures: List[str] = []

    # 1. Overlapping solar reductions compound: hour 12 keeps 0.5 * 0.2 = 0.1 of 150 kWh.
    request, directives = _fixed_case([
        _directive(0, "solar_reduction", {"hours": [12], "factor": 0.5}),
        _directive(1, "solar_reduction", {"hours": [11, 12], "factor": 0.2}),
    ])
    problems, plan = _fuzz_run(request, directives)
    failures += [f"solar_overlap: {p}" for p in problems]
    if plan is not None and plan[12].solar_used_kwh > 15.0 + 1e-6:
        failures.append(f"solar_overlap: hour 12 used {plan[12].solar_used_kwh} kWh solar, limit is 15.0 (0.5*0.2*150)")

    # 2. Reserve 300 kWh at hours 22-23 cannot coexist with neutrality (initial 200).
    #    Only that directive may be downgraded; the valid one and the no_op stay as they were.
    request, directives = _fixed_case([
        _directive(0, "minimum_battery_reserve", {"hours": [22, 23], "minimum_energy_kwh": 300.0}),
        _directive(1, "no_charge_window", {"hours": [2, 3]}),
        _directive(2, "no_op", None),
    ])
    problems, _ = _fuzz_run(request, directives)
    failures += [f"reserve_conflict: {p}" for p in problems]
    types = [d.directive_type for d in directives]
    if types != ["no_op", "no_charge_window", "no_op"] or directives[0].applies:
        failures.append(f"reserve_conflict: expected [no_op, no_charge_window, no_op], got {types}")

    # 3. Grid cap 0 all day is infeasible alone (demand 200 > discharge 100 at night);
    #    no-discharge all day is feasible alone. Fewest drops = drop only the cap.
    request, directives = _fixed_case([
        _directive(0, "max_grid_window", {"hours": list(range(24)), "max_grid_kwh": 0.0}),
        _directive(1, "no_discharge_window", {"hours": list(range(24))}),
    ])
    problems, _ = _fuzz_run(request, directives)
    failures += [f"grid_conflict: {p}" for p in problems]
    types = [d.directive_type for d in directives]
    if types != ["no_op", "no_discharge_window"]:
        failures.append(f"grid_conflict: expected [no_op, no_discharge_window], got {types}")

    return failures


def _fuzz_main(argv: List[str]) -> int:
    cases = int(argv[1]) if len(argv) > 1 else 300
    seed = int(argv[2]) if len(argv) > 2 else 2026
    rng = random.Random(seed)

    failures = _fuzz_fixed_checks()
    downgraded = 0
    slowest = 0.0
    for i in range(cases):
        request, directives = _fuzz_case(rng, f"FUZZ-{seed}-{i}")
        applied_before = sum(d.applies for d in directives)
        started = time.perf_counter()
        problems, _ = _fuzz_run(request, directives)
        slowest = max(slowest, time.perf_counter() - started)
        downgraded += applied_before - sum(d.applies for d in directives)
        failures += [f"{request.scenario_id}: {p}" for p in problems]

    print(
        f"fuzz: {cases} random + 3 fixed cases, seed={seed}, failures={len(failures)}, "
        f"downgraded directives={downgraded}, slowest request={slowest:.2f}s"
    )
    for failure in failures[:30]:
        print("  FAIL", failure)
    return 1 if failures else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)  # keep replay/optimizer logs out of the report
    sys.exit(_fuzz_main(sys.argv))
