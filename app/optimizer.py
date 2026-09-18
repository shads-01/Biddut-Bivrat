"""PuLP Linear Programming optimizer for GridWise 24-hour schedule."""

from __future__ import annotations

import logging
from typing import Dict, List, Tuple
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
