"""Independent Replay and Sanity Verification Engine for GridWise Energy Schedules.

Mirrors the judge's independent verification (§11.1–§11.3):
1. Verifies 24 discrete hours (0..23) with finite, non-negative numbers.
2. Checks hourly energy balance: grid + solar_used + discharge == demand + charge.
3. Enforces battery bounds, hourly rate limits, state continuity, and end-of-day neutrality.
4. Verifies compliance against every applied directive (solar_reduction, minimum_battery_reserve,
   no_charge_window, no_discharge_window, max_grid_window).
5. Validates that derived totals (total_grid_kwh, total_cost_bdt, peak_grid_kwh) match hourly_plan recomputations.
6. Logs any violations loudly.
"""

from __future__ import annotations

import logging
import math
from typing import Dict, List, Optional
from app.schemas import Battery, DirectiveInterpretation, HourEntry, OptimizeRequest, PlanHour

logger = logging.getLogger("gridwise.replay")

TOLERANCE = 0.01  # 0.01 kWh / 0.01 BDT tolerance specified in rules


def replay_and_verify_plan(
    request: OptimizeRequest,
    directives: List[DirectiveInterpretation],
    hourly_plan: List[PlanHour],
    total_grid_kwh: Optional[float] = None,
    total_cost_bdt: Optional[float] = None,
    peak_grid_kwh: Optional[float] = None,
) -> List[str]:
    """Independently replays and verifies that hourly_plan respects all directives and physical laws.

    Args:
        request: The incoming scenario request containing hours and battery limits.
        directives: The validated directive interpretations.
        hourly_plan: The 24-hour schedule produced by the optimizer.
        total_grid_kwh: Optional total grid energy reported in response to verify recomputation.
        total_cost_bdt: Optional total cost reported in response to verify recomputation.
        peak_grid_kwh: Optional peak grid import reported in response to verify recomputation.

    Returns:
        List of human-readable violation descriptions. Empty if plan is 100% compliant.
    """
    violations: List[str] = []
    hours_map: Dict[int, HourEntry] = {h.hour: h for h in request.hours}
    plan_map: Dict[int, PlanHour] = {p.hour: p for p in hourly_plan}
    battery: Battery = request.battery

    # 1. Plan structure check: exactly 24 hours 0..23
    if len(hourly_plan) != 24:
        violations.append(f"Hourly plan must have exactly 24 hours, found {len(hourly_plan)}")

    seen_hours = set(p.hour for p in hourly_plan)
    if seen_hours != set(range(24)):
        missing = sorted(list(set(range(24)) - seen_hours))
        violations.append(f"Hourly plan missing hours: {missing}")

    # 2. Base physical & battery rules hour by hour
    previous_energy = battery.initial_energy_kwh

    for h in range(24):
        if h not in plan_map:
            continue

        base_hour = hours_map[h]
        plan = plan_map[h]

        # Numeric sanity: finite and non-negative
        for field_name, val in [
            ("grid_kwh", plan.grid_kwh),
            ("solar_used_kwh", plan.solar_used_kwh),
            ("battery_kwh", plan.battery_kwh),
            ("battery_energy_after_kwh", plan.battery_energy_after_kwh),
        ]:
            if not math.isfinite(val):
                violations.append(f"Hour {h}: {field_name} is non-finite ({val})")
            elif val < -TOLERANCE:
                violations.append(f"Hour {h}: {field_name} is negative ({val:.4f} kWh)")

        charge_kwh = plan.battery_kwh if plan.battery_action == "charge" else 0.0
        discharge_kwh = plan.battery_kwh if plan.battery_action == "discharge" else 0.0

        if plan.battery_action not in ("charge", "discharge", "idle"):
            violations.append(f"Hour {h}: invalid battery_action '{plan.battery_action}'")

        if plan.battery_action == "idle" and plan.battery_kwh > TOLERANCE:
            violations.append(
                f"Hour {h}: battery_action is 'idle' but battery_kwh is {plan.battery_kwh:.4f} > 0"
            )

        # 2a. Rate limits
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

        # 2b. Energy balance: grid + solar_used + discharge == demand + charge
        supply = plan.grid_kwh + plan.solar_used_kwh + discharge_kwh
        consumption = base_hour.demand_kwh + charge_kwh
        if abs(supply - consumption) > TOLERANCE:
            violations.append(
                f"Hour {h}: energy balance violation. Supply={supply:.4f} kWh (grid={plan.grid_kwh:.4f}, "
                f"solar={plan.solar_used_kwh:.4f}, discharge={discharge_kwh:.4f}) != "
                f"Consumption={consumption:.4f} kWh (demand={base_hour.demand_kwh:.4f}, charge={charge_kwh:.4f})"
            )

        # 2c. Solar usage bound: 0 <= solar_used <= base_solar
        if plan.solar_used_kwh > base_hour.solar_kwh + TOLERANCE:
            violations.append(
                f"Hour {h}: solar_used_kwh {plan.solar_used_kwh:.4f} exceeds available solar "
                f"{base_hour.solar_kwh:.4f} kWh"
            )

        # 2d. Battery energy continuity: E[h] == E[h-1] + charge - discharge
        expected_energy_after = previous_energy + charge_kwh - discharge_kwh
        if abs(plan.battery_energy_after_kwh - expected_energy_after) > TOLERANCE:
            violations.append(
                f"Hour {h}: battery energy continuity failed. Reported {plan.battery_energy_after_kwh:.4f} kWh, "
                f"expected {expected_energy_after:.4f} kWh (prev={previous_energy:.4f}, +{charge_kwh:.4f} -{discharge_kwh:.4f})"
            )

        # 2e. Battery bounds (base capacity and base minimum)
        if plan.battery_energy_after_kwh < battery.minimum_energy_kwh - TOLERANCE:
            violations.append(
                f"Hour {h}: battery_energy_after {plan.battery_energy_after_kwh:.4f} kWh below base minimum "
                f"{battery.minimum_energy_kwh:.4f} kWh"
            )
        if plan.battery_energy_after_kwh > battery.capacity_kwh + TOLERANCE:
            violations.append(
                f"Hour {h}: battery_energy_after {plan.battery_energy_after_kwh:.4f} kWh exceeds capacity "
                f"{battery.capacity_kwh:.4f} kWh"
            )

        previous_energy = plan.battery_energy_after_kwh

    # 3. End-of-day neutrality (hard rule: final energy == initial energy)
    if 23 in plan_map:
        final_energy = plan_map[23].battery_energy_after_kwh
        if abs(final_energy - battery.initial_energy_kwh) > TOLERANCE:
            violations.append(
                f"End-of-day neutrality violated: final battery energy {final_energy:.4f} kWh != "
                f"initial battery energy {battery.initial_energy_kwh:.4f} kWh"
            )

    # 4. Replay against every active directive individually per CONTEXT.md §Final replay
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
                            f"> allowed {allowed_solar:.4f} (base={hours_map[h].solar_kwh:.4f}, factor={factor:.4f})"
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

    # 5. Optional check: Derived totals recomputation match
    if total_grid_kwh is not None:
        recomputed_grid = sum(p.grid_kwh for p in hourly_plan)
        if abs(total_grid_kwh - recomputed_grid) > TOLERANCE:
            violations.append(
                f"Reported total_grid_kwh ({total_grid_kwh:.4f}) does not match recomputed "
                f"sum ({recomputed_grid:.4f})"
            )

    if total_cost_bdt is not None:
        recomputed_cost = sum(p.grid_kwh * hours_map[p.hour].tariff_bdt_per_kwh for p in hourly_plan if p.hour in hours_map)
        if abs(total_cost_bdt - recomputed_cost) > TOLERANCE:
            violations.append(
                f"Reported total_cost_bdt ({total_cost_bdt:.4f}) does not match recomputed "
                f"cost ({recomputed_cost:.4f})"
            )

    if peak_grid_kwh is not None:
        recomputed_peak = max((p.grid_kwh for p in hourly_plan), default=0.0)
        if abs(peak_grid_kwh - recomputed_peak) > TOLERANCE:
            violations.append(
                f"Reported peak_grid_kwh ({peak_grid_kwh:.4f}) does not match recomputed "
                f"peak ({recomputed_peak:.4f})"
            )

    if violations:
        logger.error(
            f"Replay found {len(violations)} violations:\n"
            + "\n".join(f" - {v}" for v in violations)
        )
    else:
        logger.info("Replay verification passed: plan is 100% compliant with all directives and physical laws.")

    return violations
