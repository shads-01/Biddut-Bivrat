"""Pydantic v2 schemas and guardrail data contracts for GridWise."""

from __future__ import annotations

import math
from typing import Annotated, Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# --- Directive Enums & Adjustments ---

DirectiveType = Literal[
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
]

BatteryAction = Literal["charge", "discharge", "idle"]


def _validate_hours_list(hours: Optional[List[int]]) -> Optional[List[int]]:
    if hours is None:
        return None
    if not isinstance(hours, list):
        raise ValueError("hours must be a list of integers")
    if not hours:
        raise ValueError("hours list cannot be empty")
    for h in hours:
        if not isinstance(h, int) or isinstance(h, bool):
            raise ValueError(f"Each hour must be an integer, got: {h}")
        if h < 0 or h > 23:
            raise ValueError(f"Hour out of bounds (0-23): {h}")
    if len(hours) != len(set(hours)):
        raise ValueError("hours list must contain unique integers")
    if hours != sorted(hours):
        raise ValueError("hours list must be in strictly ascending order")
    return hours


class SolarReductionAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: List[int]
    factor: float = Field(..., ge=0.0, le=1.0)

    @field_validator("hours")
    @classmethod
    def validate_hours(cls, v: List[int]) -> List[int]:
        res = _validate_hours_list(v)
        assert res is not None
        return res

    @field_validator("factor")
    @classmethod
    def validate_factor(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("factor must be a finite number")
        if v < 0.0 or v > 1.0:
            raise ValueError("factor must be within [0.0, 1.0]")
        return v


class MinimumBatteryReserveAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: List[int]
    minimum_energy_kwh: float = Field(..., ge=0.0)

    @field_validator("hours")
    @classmethod
    def validate_hours(cls, v: List[int]) -> List[int]:
        res = _validate_hours_list(v)
        assert res is not None
        return res

    @field_validator("minimum_energy_kwh")
    @classmethod
    def validate_min_energy(cls, v: float) -> float:
        if not math.isfinite(v) or v < 0.0:
            raise ValueError("minimum_energy_kwh must be a finite non-negative number")
        return v


class WindowHoursAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: List[int]

    @field_validator("hours")
    @classmethod
    def validate_hours(cls, v: List[int]) -> List[int]:
        res = _validate_hours_list(v)
        assert res is not None
        return res


class MaxGridWindowAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: List[int]
    max_grid_kwh: float = Field(..., ge=0.0)

    @field_validator("hours")
    @classmethod
    def validate_hours(cls, v: List[int]) -> List[int]:
        res = _validate_hours_list(v)
        assert res is not None
        return res

    @field_validator("max_grid_kwh")
    @classmethod
    def validate_max_grid(cls, v: float) -> float:
        if not math.isfinite(v) or v < 0.0:
            raise ValueError("max_grid_kwh must be a finite non-negative number")
        return v


StructuredAdjustmentUnion = Union[
    SolarReductionAdjustment,
    MinimumBatteryReserveAdjustment,
    WindowHoursAdjustment,
    MaxGridWindowAdjustment,
    None,
]


class DirectiveInterpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note_index: int = Field(..., ge=0)
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: Optional[Dict[str, Any]] = None
    explanation: str = Field(default="")

    @model_validator(mode="after")
    def validate_directive_consistency(self) -> DirectiveInterpretation:
        if self.directive_type == "no_op":
            if self.applies:
                raise ValueError("no_op directive must have applies: false")
            if self.structured_adjustment is not None:
                raise ValueError("no_op directive must have structured_adjustment: null")
        else:
            if not self.applies:
                raise ValueError(f"{self.directive_type} directive must have applies: true")
            if self.structured_adjustment is None:
                raise ValueError(f"{self.directive_type} directive requires structured_adjustment")

            # Validate specific structured_adjustment shape
            adj = self.structured_adjustment
            if self.directive_type == "solar_reduction":
                parsed = SolarReductionAdjustment.model_validate(adj)
                self.structured_adjustment = parsed.model_dump()
            elif self.directive_type == "minimum_battery_reserve":
                parsed = MinimumBatteryReserveAdjustment.model_validate(adj)
                self.structured_adjustment = parsed.model_dump()
            elif self.directive_type in ("no_charge_window", "no_discharge_window"):
                parsed = WindowHoursAdjustment.model_validate(adj)
                self.structured_adjustment = parsed.model_dump()
            elif self.directive_type == "max_grid_window":
                parsed = MaxGridWindowAdjustment.model_validate(adj)
                self.structured_adjustment = parsed.model_dump()
            else:
                raise ValueError(f"Unsupported directive_type: {self.directive_type}")

        return self


# --- Request Models ---

class HourEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hour: int = Field(..., ge=0, le=23)
    demand_kwh: float = Field(..., ge=0.0)
    solar_kwh: float = Field(..., ge=0.0)
    tariff_bdt_per_kwh: float = Field(..., ge=0.0)

    @field_validator("demand_kwh", "solar_kwh", "tariff_bdt_per_kwh")
    @classmethod
    def check_finite(cls, v: float) -> float:
        if not math.isfinite(v) or v < 0:
            raise ValueError("Values must be finite and non-negative")
        return v


class Battery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capacity_kwh: float = Field(..., gt=0.0)
    initial_energy_kwh: float = Field(..., ge=0.0)
    minimum_energy_kwh: float = Field(..., ge=0.0)
    max_charge_kwh_per_hour: float = Field(..., ge=0.0)
    max_discharge_kwh_per_hour: float = Field(..., ge=0.0)

    @model_validator(mode="after")
    def validate_battery_limits(self) -> Battery:
        if not math.isfinite(self.capacity_kwh):
            raise ValueError("capacity_kwh must be finite")
        if self.minimum_energy_kwh > self.capacity_kwh:
            raise ValueError("minimum_energy_kwh cannot exceed capacity_kwh")
        if self.initial_energy_kwh < self.minimum_energy_kwh or self.initial_energy_kwh > self.capacity_kwh:
            raise ValueError("initial_energy_kwh must be within [minimum_energy_kwh, capacity_kwh]")
        return self


class OptimizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_id: str = Field(..., min_length=1)
    operator_notes: List[str] = Field(..., min_length=1, max_length=3)
    hours: List[HourEntry] = Field(..., min_length=24, max_length=24)
    battery: Battery

    @field_validator("operator_notes")
    @classmethod
    def validate_notes(cls, v: List[str]) -> List[str]:
        for idx, note in enumerate(v):
            if not isinstance(note, str) or not note.strip():
                raise ValueError(f"operator_note at index {idx} must be a non-empty string")
        return v

    @field_validator("hours")
    @classmethod
    def validate_hours_sequence(cls, v: List[HourEntry]) -> List[HourEntry]:
        if len(v) != 24:
            raise ValueError("Exactly 24 hours (0..23) are required")
        seen_hours = set()
        for entry in v:
            if entry.hour in seen_hours:
                raise ValueError(f"Duplicate hour {entry.hour} found in hours array")
            seen_hours.add(entry.hour)
        if seen_hours != set(range(24)):
            raise ValueError("hours array must cover exactly hours 0 through 23")
        return sorted(v, key=lambda h: h.hour)


# --- Response Models ---

class PlanHour(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hour: int = Field(..., ge=0, le=23)
    grid_kwh: float = Field(..., ge=0.0)
    solar_used_kwh: float = Field(..., ge=0.0)
    battery_action: BatteryAction
    battery_kwh: float = Field(..., ge=0.0)
    battery_energy_after_kwh: float = Field(..., ge=0.0)


class OptimizeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario_id: str
    directive_interpretation: List[DirectiveInterpretation]
    hourly_plan: List[PlanHour] = Field(..., min_length=24, max_length=24)
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
