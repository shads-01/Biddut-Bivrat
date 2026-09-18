"""Regression tests: the regex fallback must read a bare hour next to an am/pm hour correctly."""

import pytest

from app.fallback_parser import extract_hours_window, parse_operator_note_fallback


@pytest.mark.parametrize("note,hours", [
    ("during the 1-3 PM maintenance window", [13, 14]),
    ("between 2-4 PM", [14, 15]),
    ("from 1 to 3 PM", [13, 14]),
    ("between 1 and 3 PM", [13, 14]),
    ("from 9 to 11 AM", [9, 10]),
    ("from 11 to 3 PM", [11, 12, 13, 14]),
    ("from 10 to 2 AM", [0, 1, 22, 23]),
    ("from 2 PM until 5", [14, 15, 16]),
    ("from 12 to 2 PM", [12, 13]),
    ("13:00 to 15:00", [13, 14]),
    ("from 1 PM to 3 PM", [13, 14]),
    ("from 10 PM to 2 AM", [0, 1, 22, 23]),
    ("from noon until 2 PM", [12, 13]),
])
def test_range_hours(note, hours):
    assert extract_hours_window(note) == hours


def test_dash_range_in_full_directive():
    result = parse_operator_note_fallback(
        "Expect an 80% reduction in rooftop solar during the 1-3 PM maintenance window.", 0, 500.0
    )
    assert result["directive_type"] == "solar_reduction"
    assert result["structured_adjustment"] == {"hours": [13, 14], "factor": pytest.approx(0.2, abs=1e-4)}

    result = parse_operator_note_fallback("Do not charge the battery between 2-4 PM.", 0, 500.0)
    assert result["directive_type"] == "no_charge_window"
    assert result["structured_adjustment"] == {"hours": [14, 15]}
