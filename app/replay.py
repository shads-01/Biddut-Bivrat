"""Replay and sanity verification of generated energy plan against rules and directives."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional
from app.schemas import Battery, DirectiveInterpretation, HourEntry, OptimizeRequest, PlanHour

logger = logging.getLogger("gridwise.replay")

TOLERANCE = 0.01  # 0.01 kWh / 0.01 BDT tolerance specified in rules


def replay_and_verify_plan(
    request: OptimizeRequest,
    directives: List[DirectiveInterpretation],
    hourly_plan: List[PlanHour],
) -> List[str]:
    """Independently replays and verifies that hourly_plan respects all directives and base physics rules.

    Returns:
        List of human-readable violation descriptions. Empty if plan is 100% compliant.
    """
    violations: List[str] = []
    hours_map: Dict[int, HourEntry] = {h.hour: h for h in request.hours}
    plan_map: Dict[int, PlanHour] = {p.hour: p for p in hourly_plan}
    battery: Battery = request.battery

    if len(hourly_plan) != 24:
        violations.append(f"Hourly plan must have exactly 24 hours, found {len(hourly_plan)}")

    # Check 1: Base physical & battery rules hour by hour
    previous_energy = battery.initial_energy_kwh

    for h in range(24):
        if h not in plan_map:
            violations.append(f"Missing hour {h} in hourly plan")
            continue

        base_hour = hours_map[h]
        plan = plan_map[h]

        charge_kwh = plan.battery_kwh if plan.battery_action == "charge" else 0.0
        discharge_kwh = plan.battery_kwh if plan.battery_action == "discharge" else 0.0

        if plan.battery_action == "idle" and plan.battery_kwh > TOLERANCE:
            violations.append(
                f"Hour {h}: battery_action is 'idle' but battery_kwh is {plan.battery_kwh} > 0"
            )

        # 1a. Rate limits
        if charge_kwh > battery.max_charge_kwh_per_hour + TOLERANCE:
            violations.append(
                f"Hour {h}: charge rate {charge_kwh:.4f} kWh exceeds max_charge "
                f"{battery.max_charge_kwh_per_hour:.4f} kWh"
            )
        if discharge_kwh > battery.max_discharge_kwh_per_hour + TOLERANCE:
            violations.append(
                f"Hour {h}: discharge rate {discharge_kwh:.4f} kWh exceeds max_discharge "
                f"{battery.max_discharge_kwh_per_hour:.4f} kWh"
            )

        # 1b. Energy balance: grid + solar_used + discharge == demand + charge
        supply = plan.grid_kwh + plan.solar_used_kwh + discharge_kwh
        consumption = base_hour.demand_kwh + charge_kwh
        if abs(supply - consumption) > TOLERANCE:
            violations.append(
                f"Hour {h}: energy balance violation. Supply={supply:.4f} kWh (grid={plan.grid_kwh}, "
                f"solar={plan.solar_used_kwh}, discharge={discharge_kwh}) != Consumption={consumption:.4f} kWh "
                f"(demand={base_hour.demand_kwh}, charge={charge_kwh})"
            )

        # 1c. Solar usage bound: 0 <= solar_used <= base_solar
        if plan.solar_used_kwh > base_hour.solar_kwh + TOLERANCE:
            violations.append(
                f"Hour {h}: solar_used_kwh {plan.solar_used_kwh:.4f} exceeds available solar {base_hour.solar_kwh:.4f}"
            )

        # 1d. Battery energy continuity
        expected_energy_after = previous_energy + charge_kwh - discharge_kwh
        if abs(plan.battery_energy_after_kwh - expected_energy_after) > TOLERANCE:
            violations.append(
                f"Hour {h}: battery energy continuity failed. Reported {plan.battery_energy_after_kwh:.4f}, "
                f"expected {expected_energy_after:.4f} (prev={previous_energy:.4f}, +{charge_kwh:.4f} -{discharge_kwh:.4f})"
            )

        # 1e. Battery bounds
        if plan.battery_energy_after_kwh < battery.minimum_energy_kwh - TOLERANCE:
            violations.append(
                f"Hour {h}: battery_energy_after {plan.battery_energy_after_kwh:.4f} below base minimum "
                f"{battery.minimum_energy_kwh:.4f} kWh"
            )
        if plan.battery_energy_after_kwh > battery.capacity_kwh + TOLERANCE:
            violations.append(
                f"Hour {h}: battery_energy_after {plan.battery_energy_after_kwh:.4f} exceeds capacity "
                f"{battery.capacity_kwh:.4f} kWh"
            )

        previous_energy = plan.battery_energy_after_kwh

    # Check 2: End-of-day neutrality (hard rule: final energy == initial energy)
    if 23 in plan_map:
        final_energy = plan_map[23].battery_energy_after_kwh
        if abs(final_energy - battery.initial_energy_kwh) > TOLERANCE:
            violations.append(
                f"End-of-day neutrality violated: final battery energy {final_energy:.4f} kWh != "
                f"initial battery energy {battery.initial_energy_kwh:.4f} kWh"
            )

    # Check 3: Replay against all applied directives
    for d in directives:
        if not d.applies or not d.structured_adjustment:
            continue

        adj = d.structured_adjustment
        adj_hours = adj.get("hours", [])

        if d.directive_type == "solar_reduction":
            factor = float(adj.get("factor", 1.0))
            for h in adj_hours:
                if h in plan_map:
                    allowed_solar = hours_map[h].solar_kwh * factor
                    actual_solar = plan_map[h].solar_used_kwh
                    if actual_solar > allowed_solar + TOLERANCE:
                        violations.append(
                            f"Directive violation (solar_reduction) hour {h}: solar used {actual_solar:.4f} "
                            f"> allowed {allowed_solar:.4f} (base={hours_map[h].solar_kwh}, factor={factor})"
                        )

        elif d.directive_type == "minimum_battery_reserve":
            min_energy = float(adj.get("minimum_energy_kwh", 0.0))
            for h in adj_hours:
                if h in plan_map:
                    actual_energy = plan_map[h].battery_energy_after_kwh
                    if actual_energy < min_energy - TOLERANCE:
                        violations.append(
                            f"Directive violation (minimum_battery_reserve) hour {h}: battery energy "
                            f"{actual_energy:.4f} < required reserve {min_energy:.4f} kWh"
                        )

        elif d.directive_type == "no_charge_window":
            for h in adj_hours:
                if h in plan_map:
                    if plan_map[h].battery_action == "charge" and plan_map[h].battery_kwh > TOLERANCE:
                        violations.append(
                            f"Directive violation (no_charge_window) hour {h}: charged {plan_map[h].battery_kwh:.4f} kWh"
                        )

        elif d.directive_type == "no_discharge_window":
            for h in adj_hours:
                if h in plan_map:
                    if plan_map[h].battery_action == "discharge" and plan_map[h].battery_kwh > TOLERANCE:
                        violations.append(
                            f"Directive violation (no_discharge_window) hour {h}: discharged {plan_map[h].battery_kwh:.4f} kWh"
                        )

        elif d.directive_type == "max_grid_window":
            max_grid = float(adj.get("max_grid_kwh", 0.0))
            for h in adj_hours:
                if h in plan_map:
                    actual_grid = plan_map[h].grid_kwh
                    if actual_grid > max_grid + TOLERANCE:
                        violations.append(
                            f"Directive violation (max_grid_window) hour {h}: grid import {actual_grid:.4f} "
                            f"> limit {max_grid:.4f} kWh"
                        )

    if violations:
        logger.error(f"Replay found {len(violations)} violations:\n" + "\n".join(f" - {v}" for v in violations))
    else:
        logger.info("Replay verification passed: plan is 100% compliant with all directives and base physics.")

    return violations
