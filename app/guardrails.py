"""Deterministic guardrails for validating LLM directive interpretations.

Enforces strict boundary contracts between untrusted LLM outputs and the optimizer:
1. Validates all directive types against the 6 canonical types.
2. Enforces unique ascending hours (0-23) without gaps or booleans.
3. Clamps/validates factor in [0.0, 1.0].
4. Enforces minimum_battery_reserve <= battery.capacity_kwh.
5. Ensures exact 0..N-1 note coverage with no duplicates.
6. Builds sanitized structured_adjustment dicts in code with exact required keys.
7. Safely degrades any malformed entry to no_op without failing the entire request.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Dict, List, Optional

from app.schemas import Battery, DirectiveInterpretation, DirectiveType

logger = logging.getLogger("gridwise.guardrails")

ALLOWED_DIRECTIVE_TYPES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}


def make_safe_no_op(note_index: int, explanation: str) -> DirectiveInterpretation:
    """Constructs a deterministic safe no_op directive when parsing/validation fails."""
    return DirectiveInterpretation(
        note_index=note_index,
        applies=False,
        directive_type="no_op",
        structured_adjustment=None,
        explanation=str(explanation),
    )


def _strip_markdown_code_fences(text: str) -> str:
    """Strips markdown code blocks like ```json ... ``` from LLM response text."""
    trimmed = text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", trimmed, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return trimmed


def _clean_hours_list(raw_hours: Any) -> Optional[List[int]]:
    """Validates and cleans an hours array.

    Must be:
    - Non-empty list of integers
    - Strictly in 0..23 range
    - Unique integers
    - Strictly ascending order
    - No booleans or float values
    """
    if not isinstance(raw_hours, list) or isinstance(raw_hours, (str, bytes)):
        return None
    if not raw_hours:
        return None

    cleaned_hours: List[int] = []
    for item in raw_hours:
        # In Python, bool is a subclass of int: isinstance(True, int) is True
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        if isinstance(item, float):
            if not item.is_integer():
                return None
            item = int(item)
        if item < 0 or item > 23:
            return None
        cleaned_hours.append(item)

    # Must be unique
    if len(cleaned_hours) != len(set(cleaned_hours)):
        return None

    # Must be in strictly ascending order
    if cleaned_hours != sorted(cleaned_hours):
        return None

    return cleaned_hours


def _sanitize_directive_item(
    raw_item: Dict[str, Any],
    note_index: int,
    battery: Battery,
    note_text: str,
) -> DirectiveInterpretation:
    """Sanitizes and constructs a strict DirectiveInterpretation from an untrusted dictionary item."""
    raw_type = raw_item.get("directive_type")
    raw_explanation = str(raw_item.get("explanation") or raw_item.get("reasoning") or "").strip()
    if not raw_explanation:
        raw_explanation = f"Interpreted directive for note: {note_text}"

    if raw_type not in ALLOWED_DIRECTIVE_TYPES or raw_type == "no_op":
        return make_safe_no_op(
            note_index=note_index,
            explanation=raw_explanation if raw_type == "no_op" else f"Unsupported directive type '{raw_type}'; degraded to no_op",
        )

    # Extract structured adjustment payload
    raw_adj = raw_item.get("structured_adjustment")
    if not isinstance(raw_adj, dict):
        logger.warning(f"Note {note_index}: non-dict structured_adjustment for {raw_type}")
        return make_safe_no_op(note_index, f"Missing or invalid structured_adjustment for {raw_type}")

    # Validate hours for all windowed directive types
    hours = _clean_hours_list(raw_adj.get("hours"))
    if hours is None:
        logger.warning(f"Note {note_index}: invalid hours array {raw_adj.get('hours')} for {raw_type}")
        return make_safe_no_op(note_index, f"Invalid hours list for {raw_type}")

    # Build clean adjustments with exact keys
    if raw_type == "solar_reduction":
        raw_factor = raw_adj.get("factor")
        try:
            factor = float(raw_factor)
            if not math.isfinite(factor) or factor < 0.0 or factor > 1.0:
                raise ValueError("factor out of range [0.0, 1.0]")
            factor = round(factor, 4)
        except (ValueError, TypeError) as e:
            logger.warning(f"Note {note_index}: invalid solar reduction factor {raw_factor}: {e}")
            return make_safe_no_op(note_index, f"Invalid factor for solar_reduction: {raw_factor}")

        return DirectiveInterpretation(
            note_index=note_index,
            applies=True,
            directive_type="solar_reduction",
            structured_adjustment={"hours": hours, "factor": factor},
            explanation=raw_explanation,
        )

    elif raw_type == "minimum_battery_reserve":
        raw_min_energy = raw_adj.get("minimum_energy_kwh")
        try:
            min_energy = float(raw_min_energy)
            if not math.isfinite(min_energy) or min_energy < 0.0:
                raise ValueError("minimum_energy_kwh must be non-negative finite number")
            if min_energy > battery.capacity_kwh:
                logger.warning(
                    f"Note {note_index}: minimum_energy_kwh ({min_energy}) exceeds battery capacity ({battery.capacity_kwh})"
                )
                return make_safe_no_op(
                    note_index,
                    f"Requested reserve {min_energy} kWh exceeds battery capacity {battery.capacity_kwh} kWh.",
                )
            min_energy = round(min_energy, 4)
        except (ValueError, TypeError) as e:
            logger.warning(f"Note {note_index}: invalid minimum_energy_kwh {raw_min_energy}: {e}")
            return make_safe_no_op(note_index, f"Invalid minimum_energy_kwh: {raw_min_energy}")

        return DirectiveInterpretation(
            note_index=note_index,
            applies=True,
            directive_type="minimum_battery_reserve",
            structured_adjustment={"hours": hours, "minimum_energy_kwh": min_energy},
            explanation=raw_explanation,
        )

    elif raw_type in ("no_charge_window", "no_discharge_window"):
        return DirectiveInterpretation(
            note_index=note_index,
            applies=True,
            directive_type=raw_type,  # type: ignore[arg-type]
            structured_adjustment={"hours": hours},
            explanation=raw_explanation,
        )

    elif raw_type == "max_grid_window":
        raw_max_grid = raw_adj.get("max_grid_kwh")
        try:
            max_grid = float(raw_max_grid)
            if not math.isfinite(max_grid) or max_grid < 0.0:
                raise ValueError("max_grid_kwh must be non-negative finite number")
            max_grid = round(max_grid, 4)
        except (ValueError, TypeError) as e:
            logger.warning(f"Note {note_index}: invalid max_grid_kwh {raw_max_grid}: {e}")
            return make_safe_no_op(note_index, f"Invalid max_grid_kwh: {raw_max_grid}")

        return DirectiveInterpretation(
            note_index=note_index,
            applies=True,
            directive_type="max_grid_window",
            structured_adjustment={"hours": hours, "max_grid_kwh": max_grid},
            explanation=raw_explanation,
        )

    return make_safe_no_op(note_index, f"Unrecognized directive structure: {raw_type}")


def validate_and_guard_interpretations(
    raw_llm_output: Optional[str],
    operator_notes: List[str],
    battery: Battery,
) -> List[DirectiveInterpretation]:
    """Validates untrusted raw LLM output against schemas and physical battery constraints.

    Ensures:
    1. Exactly one entry per note in operator_notes.
    2. note_index covers 0..N-1 with no duplicates and no gaps.
    3. Conforms strictly to DirectiveInterpretation with clean, exact keys.
    4. For minimum_battery_reserve, minimum_energy_kwh <= battery.capacity_kwh.
    5. In case of any parsing, typing, or schema failure, safely degrades to no_op.
    """
    total_notes = len(operator_notes)
    validated_map: Dict[int, DirectiveInterpretation] = {}

    if not raw_llm_output or not str(raw_llm_output).strip():
        logger.warning("LLM output is empty or unavailable; applying safe no_op fallbacks.")
        return [
            make_safe_no_op(idx, f"LLM output unavailable for note: {operator_notes[idx]}")
            for idx in range(total_notes)
        ]

    cleaned_text = _strip_markdown_code_fences(raw_llm_output)
    parsed_json: Optional[Any] = None
    try:
        parsed_json = json.loads(cleaned_text)
    except Exception as e:
        logger.warning(f"Failed to parse LLM JSON: {e}. Raw content: {raw_llm_output[:200]}")
        return [
            make_safe_no_op(idx, f"Parse error in LLM output; safely ignored note: {operator_notes[idx]}")
            for idx in range(total_notes)
        ]

    # Support multiple JSON container shapes: {"interpretations": [...]}, {"directive_interpretation": [...]}, {"directives": [...]}, or raw list
    items: List[Any] = []
    if isinstance(parsed_json, dict):
        if "interpretations" in parsed_json and isinstance(parsed_json["interpretations"], list):
            items = parsed_json["interpretations"]
        elif "directive_interpretation" in parsed_json and isinstance(parsed_json["directive_interpretation"], list):
            items = parsed_json["directive_interpretation"]
        elif "directives" in parsed_json and isinstance(parsed_json["directives"], list):
            items = parsed_json["directives"]
        elif "directive_type" in parsed_json:
            # Single interpretation dict
            items = [parsed_json]
    elif isinstance(parsed_json, list):
        items = parsed_json

    for idx, raw_item in enumerate(items):
        if not isinstance(raw_item, dict):
            continue

        raw_idx = raw_item.get("note_index")
        # If note_index is omitted, infer from array position if within bounds
        if raw_idx is None or isinstance(raw_idx, bool) or not isinstance(raw_idx, int):
            if idx < total_notes:
                raw_idx = idx
            else:
                continue

        if raw_idx < 0 or raw_idx >= total_notes:
            continue

        # Prevent duplicates — first valid interpretation takes priority
        if raw_idx in validated_map:
            logger.warning(f"Duplicate note_index {raw_idx} from LLM; keeping earlier valid entry.")
            continue

        try:
            directive = _sanitize_directive_item(
                raw_item=raw_item,
                note_index=raw_idx,
                battery=battery,
                note_text=operator_notes[raw_idx],
            )
            # Final validation via Pydantic model
            directive = DirectiveInterpretation.model_validate(directive.model_dump())
            validated_map[raw_idx] = directive
        except Exception as e:
            logger.warning(f"Guardrail rejection on note_index {raw_idx}: {e}. Falling back to safe no_op.")
            validated_map[raw_idx] = make_safe_no_op(
                raw_idx,
                f"Guardrail fallback: invalid directive format ({str(e)})",
            )

    # Fill any missing notes in 0..N-1
    result: List[DirectiveInterpretation] = []
    for idx in range(total_notes):
        if idx in validated_map:
            result.append(validated_map[idx])
        else:
            logger.warning(f"Missing interpretation for note_index {idx}; creating safe no_op fallback.")
            result.append(
                make_safe_no_op(
                    idx,
                    f"No valid directive provided by model for note: {operator_notes[idx]}",
                )
            )

    return result
