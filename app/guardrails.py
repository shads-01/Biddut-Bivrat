"""Deterministic guardrails for validating LLM directive interpretations."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from app.schemas import Battery, DirectiveInterpretation

logger = logging.getLogger("gridwise.guardrails")


def make_safe_no_op(note_index: int, explanation: str) -> DirectiveInterpretation:
    """Constructs a deterministic safe no_op directive when parsing/validation fails."""
    return DirectiveInterpretation(
        note_index=note_index,
        applies=False,
        directive_type="no_op",
        structured_adjustment=None,
        explanation=explanation,
    )


def validate_and_guard_interpretations(
    raw_llm_output: Optional[str],
    operator_notes: List[str],
    battery: Battery,
) -> List[DirectiveInterpretation]:
    """Validates raw LLM response against schemas and scenario constraints.

    Ensures:
    1. Exactly one entry per note in operator_notes.
    2. note_index covers 0..N-1 with no duplicates and no gaps.
    3. Conforms strictly to DirectiveInterpretation and adjustment schemas.
    4. For minimum_battery_reserve, minimum_energy_kwh <= battery.capacity_kwh.
    5. In case of any parsing or validation failure, safely degrades to no_op.
    """
    total_notes = len(operator_notes)
    validated_map: Dict[int, DirectiveInterpretation] = {}

    if not raw_llm_output:
        logger.warning("LLM output is empty or unavailable; applying safe no_op fallbacks.")
        return [
            make_safe_no_op(idx, f"LLM output unavailable for note: {operator_notes[idx]}")
            for idx in range(total_notes)
        ]

    parsed_json: Optional[Any] = None
    try:
        parsed_json = json.loads(raw_llm_output)
    except Exception as e:
        logger.warning(f"Failed to parse LLM JSON: {e}. Output was: {raw_llm_output[:200]}")
        return [
            make_safe_no_op(idx, f"Parse error in LLM output; safely ignored note: {operator_notes[idx]}")
            for idx in range(total_notes)
        ]

    # Support {"interpretations": [...]} or raw list [...]
    items: List[Any] = []
    if isinstance(parsed_json, dict):
        if "interpretations" in parsed_json and isinstance(parsed_json["interpretations"], list):
            items = parsed_json["interpretations"]
        elif "directive_interpretation" in parsed_json and isinstance(parsed_json["directive_interpretation"], list):
            items = parsed_json["directive_interpretation"]
        else:
            # Maybe the dict is a single interpretation item
            items = [parsed_json]
    elif isinstance(parsed_json, list):
        items = parsed_json
    else:
        items = []

    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue

        raw_idx = raw_item.get("note_index")
        if raw_idx is None or not isinstance(raw_idx, int) or isinstance(raw_idx, bool):
            continue

        if raw_idx < 0 or raw_idx >= total_notes:
            continue

        # Do not allow duplicate note_index entries
        if raw_idx in validated_map:
            logger.warning(f"Duplicate note_index {raw_idx} from LLM; keeping earlier valid interpretation.")
            continue

        try:
            directive = DirectiveInterpretation.model_validate(raw_item)

            # Scenario-specific constraint check:
            # minimum_battery_reserve cannot exceed scenario battery capacity
            if directive.directive_type == "minimum_battery_reserve" and directive.structured_adjustment:
                min_kwh = directive.structured_adjustment.get("minimum_energy_kwh", 0.0)
                if min_kwh > battery.capacity_kwh:
                    logger.warning(
                        f"Directive minimum_energy_kwh ({min_kwh}) exceeds battery capacity "
                        f"({battery.capacity_kwh}). Falling back to safe no_op."
                    )
                    directive = make_safe_no_op(
                        raw_idx,
                        f"Requested reserve {min_kwh} kWh exceeds battery capacity {battery.capacity_kwh} kWh.",
                    )

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
