"""PuLP Linear Programming optimizer for GridWise 24-hour schedule."""

from __future__ import annotations

import logging
import random
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import pulp

from app.schemas import (
    DirectiveInterpretation,
    OptimizeRequest,
    PlanHour,
)

logger = logging.getLogger("gridwise.optimizer")

ROUND_DP = 4  # decimals in the response; judge tolerance is 0.01


@dataclass
class _Limits:
    """Per-hour bounds after merging every applied directive (list index = hour)."""

    effective_solar: List[float]
    min_energy: List[float]
    max_charge: List[float]
    max_discharge: List[float]
    grid_cap: List[Optional[float]]


def _merge_directives(request: OptimizeRequest, directives: List[DirectiveInterpretation]) -> _Limits:
    """Folds applied directives into per-hour limits. Overlaps: solar factors multiply,
    reserves take the max, grid caps take the min, no-charge/no-discharge hours union."""
    battery = request.battery
    hours_map = {entry.hour: entry for entry in request.hours}
    solar_factor = [1.0] * 24
    min_energy = [battery.minimum_energy_kwh] * 24
    max_charge = [battery.max_charge_kwh_per_hour] * 24
    max_discharge = [battery.max_discharge_kwh_per_hour] * 24
    grid_cap: List[Optional[float]] = [None] * 24

    for d in directives:
        if not d.applies or not d.structured_adjustment:
            continue
        adj = d.structured_adjustment
        for h in adj.get("hours", []):
            if not 0 <= h < 24:
                continue
            if d.directive_type == "solar_reduction":
                # Spec 5.3 applies each factor to the solar it sees, so overlapping reductions compound.
                solar_factor[h] *= float(adj["factor"])
            elif d.directive_type == "minimum_battery_reserve":
                min_energy[h] = max(min_energy[h], float(adj["minimum_energy_kwh"]))
            elif d.directive_type == "no_charge_window":
                max_charge[h] = 0.0
            elif d.directive_type == "no_discharge_window":
                max_discharge[h] = 0.0
            elif d.directive_type == "max_grid_window":
                cap = float(adj["max_grid_kwh"])
                grid_cap[h] = cap if grid_cap[h] is None else min(grid_cap[h], cap)

    return _Limits(
        effective_solar=[hours_map[h].solar_kwh * solar_factor[h] for h in range(24)],
        min_energy=min_energy,
        max_charge=max_charge,
        max_discharge=max_discharge,
        grid_cap=grid_cap,
    )


def _solve_lp(request: OptimizeRequest, limits: _Limits) -> Optional[Dict[str, List[float]]]:
    """Solves the cost-minimizing LP under the given limits. Returns None if no optimal schedule exists."""
    battery = request.battery
    hours_map = {entry.hour: entry for entry in request.hours}
    # Fixed problem name: scenario_id may contain characters PuLP rejects in names.
    prob = pulp.LpProblem("GridWise", pulp.LpMinimize)

    grid = [pulp.LpVariable(f"grid_{h}", 0.0, limits.grid_cap[h]) for h in range(24)]
    solar = [pulp.LpVariable(f"solar_{h}", 0.0, limits.effective_solar[h]) for h in range(24)]
    charge = [pulp.LpVariable(f"charge_{h}", 0.0, limits.max_charge[h]) for h in range(24)]
    discharge = [pulp.LpVariable(f"discharge_{h}", 0.0, limits.max_discharge[h]) for h in range(24)]
    energy = [pulp.LpVariable(f"e_after_{h}", limits.min_energy[h], battery.capacity_kwh) for h in range(24)]
    peak = pulp.LpVariable("peak_grid", 0.0)

    for h in range(24):
        prob += peak >= grid[h]

    # Tie-breakers: 1e-6 on charge+discharge stops simultaneous charge/discharge churn;
    # 1e-5 on peak picks the lowest peak among equal-cost schedules.
    prob += (
        pulp.lpSum(
            grid[h] * hours_map[h].tariff_bdt_per_kwh + 1e-6 * (charge[h] + discharge[h])
            for h in range(24)
        )
        + 1e-5 * peak
    )

    for h in range(24):
        prob += grid[h] + solar[h] + discharge[h] == hours_map[h].demand_kwh + charge[h]
        before = battery.initial_energy_kwh if h == 0 else energy[h - 1]
        prob += energy[h] == before + charge[h] - discharge[h]

    # End-of-day neutrality (hard requirement, never relaxed).
    prob += energy[23] == battery.initial_energy_kwh

    status = prob.solve(pulp.PULP_CBC_CMD(msg=False))
    if status != pulp.LpStatusOptimal:
        logger.warning("LP not optimal for %s: %s", request.scenario_id, pulp.LpStatus[status])
        return None

    def value(var: pulp.LpVariable) -> float:
        return max(0.0, float(pulp.value(var) or 0.0))

    return {
        "grid": [value(v) for v in grid],
        "solar": [value(v) for v in solar],
        "charge": [value(v) for v in charge],
        "discharge": [value(v) for v in discharge],
    }


def _build_plan(request: OptimizeRequest, limits: _Limits, raw: Dict[str, List[float]]) -> List[PlanHour]:
    """Turns raw LP values into a plan whose numbers stay consistent after rounding.

    Order matters: net the battery flow into one action per hour, round, push any rounding
    drift into the last active hour so hour 23 returns exactly to the initial level, recompute
    energy from the rounded flows, then derive grid from the energy-balance equation.
    """
    hours_map = {entry.hour: entry for entry in request.hours}

    solar = [max(0.0, min(round(raw["solar"][h], ROUND_DP), limits.effective_solar[h])) for h in range(24)]
    # Positive = charge, negative = discharge, zero = idle.
    net = [round(raw["charge"][h] - raw["discharge"][h], ROUND_DP) for h in range(24)]

    drift = round(sum(net), ROUND_DP)
    if drift != 0.0:
        last_active = max((h for h in range(24) if net[h] != 0.0), default=None)
        if last_active is not None:
            net[last_active] = round(net[last_active] - drift, ROUND_DP)

    plan: List[PlanHour] = []
    energy = request.battery.initial_energy_kwh
    for h in range(24):
        charge = max(net[h], 0.0)
        discharge = max(-net[h], 0.0)
        energy = round(energy + net[h], ROUND_DP)
        grid = max(0.0, round(hours_map[h].demand_kwh + charge - solar[h] - discharge, ROUND_DP))
        action = "charge" if net[h] > 0 else "discharge" if net[h] < 0 else "idle"
        plan.append(
            PlanHour(
                hour=h,
                grid_kwh=grid,
                solar_used_kwh=solar[h],
                battery_action=action,
                battery_kwh=abs(net[h]),
                battery_energy_after_kwh=max(0.0, energy),
            )
        )
    return plan


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
    hours_map = {entry.hour: entry for entry in request.hours}
    limits = _merge_directives(request, directives)
    raw = _solve_lp(request, limits)
    if raw is None:
        raise ValueError("Optimizer could not find a feasible schedule.")
    hourly_plan = _build_plan(request, limits, raw)

    # Summary metrics strictly from hourly_plan alone
    total_grid_kwh = round(sum(p.grid_kwh for p in hourly_plan), ROUND_DP)
    total_cost_bdt = round(sum(p.grid_kwh * hours_map[p.hour].tariff_bdt_per_kwh for p in hourly_plan), ROUND_DP)
    peak_grid_kwh = round(max(p.grid_kwh for p in hourly_plan), ROUND_DP)

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
