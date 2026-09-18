"""Comprehensive unit tests for deterministic guardrails in app/guardrails.py."""

import json
import pytest
from app.guardrails import validate_and_guard_interpretations
from app.schemas import Battery, DirectiveInterpretation


@pytest.fixture
def standard_battery():
    return Battery(
        capacity_kwh=500.0,
        initial_energy_kwh=200.0,
        minimum_energy_kwh=50.0,
        max_charge_kwh_per_hour=100.0,
        max_discharge_kwh_per_hour=100.0,
    )


def test_markdown_code_fence_stripping(standard_battery):
    """Ensure markdown wrapped JSON code fences are parsed properly."""
    raw = """```json
    {
      "interpretations": [
        {
          "note_index": 0,
          "directive_type": "solar_reduction",
          "applies": true,
          "structured_adjustment": {"hours": [12, 13], "factor": 0.5},
          "explanation": "Cloud cover"
        }
      ]
    }
    ```"""
    notes = ["Cloud cover from 12 to 14"]
    res = validate_and_guard_interpretations(raw, notes, standard_battery)
    assert len(res) == 1
    assert res[0].directive_type == "solar_reduction"
    assert res[0].applies is True
    assert res[0].structured_adjustment == {"hours": [12, 13], "factor": 0.5}


def test_invalid_and_out_of_bounds_hours_downgrade_to_no_op(standard_battery):
    """Ensure invalid hours arrays (unsorted, duplicates, booleans, out of bounds, strings) safely downgrade."""
    test_hours_cases = [
        [14, 13],  # Not ascending
        [13, 13, 14],  # Duplicates
        [24],  # Out of range (>23)
        [-1],  # Out of range (<0)
        [True, False],  # Booleans
        "13-14",  # String
        [],  # Empty
        [12, "13", 14],  # Mixed types
    ]

    for bad_hours in test_hours_cases:
        raw = json.dumps(
            {
                "interpretations": [
                    {
                        "note_index": 0,
                        "directive_type": "solar_reduction",
                        "applies": True,
                        "structured_adjustment": {"hours": bad_hours, "factor": 0.5},
                        "explanation": "Test",
                    }
                ]
            }
        )
        res = validate_and_guard_interpretations(raw, ["Test note"], standard_battery)
        assert len(res) == 1
        assert res[0].directive_type == "no_op", f"Failed to downgrade bad hours: {bad_hours}"
        assert res[0].applies is False
        assert res[0].structured_adjustment is None


def test_solar_reduction_factor_bounds(standard_battery):
    """Ensure solar reduction factor must be in [0.0, 1.0] and finite."""
    bad_factors = [-0.1, 1.05, 99.0, float("nan"), float("inf"), "invalid_factor"]

    for bad_factor in bad_factors:
        raw = json.dumps(
            {
                "interpretations": [
                    {
                        "note_index": 0,
                        "directive_type": "solar_reduction",
                        "applies": True,
                        "structured_adjustment": {"hours": [10, 11], "factor": bad_factor},
                        "explanation": "Test",
                    }
                ]
            }
        )
        res = validate_and_guard_interpretations(raw, ["Test note"], standard_battery)
        assert len(res) == 1
        assert res[0].directive_type == "no_op"
        assert res[0].applies is False


def test_minimum_battery_reserve_capacity_cap(standard_battery):
    """Ensure minimum battery reserve cannot exceed battery.capacity_kwh."""
    # Within capacity -> should pass
    valid_raw = json.dumps(
        {
            "interpretations": [
                {
                    "note_index": 0,
                    "directive_type": "minimum_battery_reserve",
                    "applies": True,
                    "structured_adjustment": {"hours": [18, 19], "minimum_energy_kwh": 300.0},
                    "explanation": "Valid reserve",
                }
            ]
        }
    )
    res1 = validate_and_guard_interpretations(valid_raw, ["Note 1"], standard_battery)
    assert res1[0].directive_type == "minimum_battery_reserve"
    assert res1[0].applies is True
    assert res1[0].structured_adjustment == {"hours": [18, 19], "minimum_energy_kwh": 300.0}

    # Exceeding capacity (550 > 500) -> downgrade to no_op
    exceed_raw = json.dumps(
        {
            "interpretations": [
                {
                    "note_index": 0,
                    "directive_type": "minimum_battery_reserve",
                    "applies": True,
                    "structured_adjustment": {"hours": [18, 19], "minimum_energy_kwh": 550.0},
                    "explanation": "Excessive reserve",
                }
            ]
        }
    )
    res2 = validate_and_guard_interpretations(exceed_raw, ["Note 1"], standard_battery)
    assert res2[0].directive_type == "no_op"
    assert res2[0].applies is False
    assert res2[0].structured_adjustment is None


def test_unsupported_directive_type_downgrade(standard_battery):
    """Ensure unknown directive types are cleanly downgraded to no_op."""
    raw = json.dumps(
        {
            "interpretations": [
                {
                    "note_index": 0,
                    "directive_type": "invented_solar_boost",
                    "applies": True,
                    "structured_adjustment": {"hours": [10, 11], "factor": 2.0},
                    "explanation": "Invented directive",
                }
            ]
        }
    )
    res = validate_and_guard_interpretations(raw, ["Invented note"], standard_battery)
    assert len(res) == 1
    assert res[0].directive_type == "no_op"
    assert res[0].applies is False
    assert res[0].structured_adjustment is None


def test_partial_failure_downgrades_only_bad_note(standard_battery):
    """Ensure that if 1 out of 3 notes is invalid, only that note is downgraded while valid ones are kept."""
    raw = json.dumps(
        {
            "interpretations": [
                {
                    "note_index": 0,
                    "directive_type": "no_charge_window",
                    "applies": True,
                    "structured_adjustment": {"hours": [17, 18]},
                    "explanation": "Valid note 1",
                },
                {
                    "note_index": 1,
                    "directive_type": "solar_reduction",
                    "applies": True,
                    "structured_adjustment": {"hours": [14, 13], "factor": 0.5},  # Unsorted hours!
                    "explanation": "Invalid note 2",
                },
                {
                    "note_index": 2,
                    "directive_type": "max_grid_window",
                    "applies": True,
                    "structured_adjustment": {"hours": [19, 20], "max_grid_kwh": 80.0},
                    "explanation": "Valid note 3",
                },
            ]
        }
    )
    notes = ["Note 1", "Note 2", "Note 3"]
    res = validate_and_guard_interpretations(raw, notes, standard_battery)

    assert len(res) == 3
    # Note 0: Kept valid
    assert res[0].note_index == 0
    assert res[0].directive_type == "no_charge_window"
    assert res[0].applies is True

    # Note 1: Downgraded to no_op
    assert res[1].note_index == 1
    assert res[1].directive_type == "no_op"
    assert res[1].applies is False

    # Note 2: Kept valid
    assert res[2].note_index == 2
    assert res[2].directive_type == "max_grid_window"
    assert res[2].applies is True


def test_missing_notes_and_duplicate_indices(standard_battery):
    """Ensure duplicate indices keep the first valid entry and missing indices are filled with no_op."""
    raw = json.dumps(
        {
            "interpretations": [
                {
                    "note_index": 0,
                    "directive_type": "no_charge_window",
                    "applies": True,
                    "structured_adjustment": {"hours": [17, 18]},
                    "explanation": "First entry for note 0",
                },
                {
                    "note_index": 0,  # Duplicate!
                    "directive_type": "no_discharge_window",
                    "applies": True,
                    "structured_adjustment": {"hours": [1, 2]},
                    "explanation": "Duplicate note 0",
                },
                # Note index 1 is completely missing!
            ]
        }
    )
    notes = ["Note 0", "Note 1"]
    res = validate_and_guard_interpretations(raw, notes, standard_battery)

    assert len(res) == 2
    assert res[0].note_index == 0
    assert res[0].directive_type == "no_charge_window"

    assert res[1].note_index == 1
    assert res[1].directive_type == "no_op"
    assert res[1].applies is False


def test_empty_or_malformed_llm_output(standard_battery):
    """Ensure completely empty or unparseable output generates safe no_ops for all notes without crashing."""
    notes = ["Note A", "Note B"]

    for malformed in [None, "", "   ", "{{bad json", "['not a dict']", "42"]:
        res = validate_and_guard_interpretations(malformed, notes, standard_battery)
        assert len(res) == 2
        assert all(d.directive_type == "no_op" and not d.applies for d in res)
        assert res[0].note_index == 0
        assert res[1].note_index == 1
