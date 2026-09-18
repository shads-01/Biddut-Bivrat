"""Regression tests for: extra request fields, fallback caching, and values the code must not invent."""

import copy
import json
import types
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app.llm as llm
from app.fallback_parser import parse_operator_note_fallback
from app.main import app
from app.schemas import OptimizeRequest

client = TestClient(app)

SAMPLES = json.loads(
    (Path(__file__).parent.parent / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json").read_text(encoding="utf-8")
)["cases"]
SAMPLE = SAMPLES[0]["input"]


@pytest.fixture(autouse=True)
def clean_cache():
    llm._INTERPRETATION_CACHE.clear()
    yield
    llm._INTERPRETATION_CACHE.clear()


def fake_client(content=None, exc=None):
    """A stand-in LLM client; not an OpenAI instance, so call_llm_for_interpretations uses the mock path."""
    calls = []

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if exc:
                raise exc
            message = types.SimpleNamespace(content=content)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    stub = types.SimpleNamespace(chat=types.SimpleNamespace(completions=Completions()))
    stub.calls = calls
    return stub


def primitive(note_index=0, directive_type="no_op", **fields):
    item = {
        "note_index": note_index,
        "directive_type": directive_type,
        "window": None,
        "reduction_type": None,
        "percent": None,
        "value": None,
        "reasoning": "test",
    }
    item.update(fields)
    return item


def assemble(raw_item, note, cap=200.0):
    return llm.assemble_directive_from_primitive(raw_item, note, 0, cap)


# --- 1. Unknown request fields must not cause a 400 ---------------------------------------------

def test_extra_request_fields_are_ignored():
    with patch.object(llm, "get_llm_client", return_value=(None, "")):
        baseline = client.post("/optimize-energy", json=SAMPLE)
        body = copy.deepcopy(SAMPLE)
        body["label"] = "harness metadata"
        body["hours"][3]["note"] = "extra hour field"
        body["battery"]["chemistry"] = "LFP"
        with_extras = client.post("/optimize-energy", json=body)

    assert baseline.status_code == 200
    assert with_extras.status_code == 200
    assert with_extras.json() == baseline.json()


def test_extra_fields_do_not_leak_into_the_validated_request():
    body = copy.deepcopy(SAMPLE)
    body["label"] = "x"
    body["battery"]["chemistry"] = "LFP"
    dumped = OptimizeRequest.model_validate(body).model_dump()
    assert "label" not in dumped
    assert "chemistry" not in dumped["battery"]


def test_missing_required_fields_are_still_rejected():
    for drop in ("scenario_id", "operator_notes", "hours", "battery"):
        body = copy.deepcopy(SAMPLE)
        body.pop(drop)
        assert client.post("/optimize-energy", json=body).status_code == 400, drop


# --- 2. Fallback answers must not be cached ------------------------------------------------------

NOTE = ["Do not charge the battery between 2 PM and 4 PM."]
LLM_ANSWER = json.dumps({"interpretations": [
    primitive(0, "no_charge_window", window={"start_hour": 14, "end_hour": 16}),
]})


def test_no_client_result_is_not_cached():
    with patch.object(llm, "get_llm_client", return_value=(None, "")):
        llm.call_llm_for_interpretations(NOTE, 200.0)
    assert llm._INTERPRETATION_CACHE == {}


def test_failed_llm_call_result_is_not_cached_and_next_call_uses_llm():
    with patch.object(llm, "get_llm_client", return_value=(fake_client(exc=TimeoutError("down")), "mock")):
        first = json.loads(llm.call_llm_for_interpretations(NOTE, 200.0))
    assert first["interpretations"][0]["explanation"].startswith("Fallback rule")
    assert llm._INTERPRETATION_CACHE == {}

    healthy = fake_client(content=LLM_ANSWER)
    with patch.object(llm, "get_llm_client", return_value=(healthy, "mock")):
        second = json.loads(llm.call_llm_for_interpretations(NOTE, 200.0))
    assert len(healthy.calls) == 1
    assert second["interpretations"][0]["explanation"] == "test"


def test_successful_llm_result_is_cached():
    healthy = fake_client(content=LLM_ANSWER)
    with patch.object(llm, "get_llm_client", return_value=(healthy, "mock")):
        llm.call_llm_for_interpretations(NOTE, 200.0)
        llm.call_llm_for_interpretations(NOTE, 200.0)
    assert len(healthy.calls) == 1


# --- 3. Values the LLM path must not invent -----------------------------------------------------

VAGUE_SOLAR = "Something odd will happen to the rooftop solar between 1 PM and 3 PM."
WINDOW_13_15 = {"start_hour": 13, "end_hour": 15}


@pytest.mark.parametrize("percent", [None, "lots", float("nan"), 150, -5])
def test_solar_reduction_without_a_usable_percent_is_not_invented(percent):
    result = assemble(primitive(directive_type="solar_reduction", window=WINDOW_13_15,
                                reduction_type="remaining", percent=percent), VAGUE_SOLAR)
    assert result["directive_type"] == "no_op"
    assert result["applies"] is False and result["structured_adjustment"] is None


@pytest.mark.parametrize("reduction_type", [None, "sideways", ""])
def test_unknown_reduction_type_is_not_treated_as_reduced_by(reduction_type):
    result = assemble(primitive(directive_type="solar_reduction", window=WINDOW_13_15,
                                reduction_type=reduction_type, percent=20), VAGUE_SOLAR)
    assert result["directive_type"] == "no_op"


def test_unknown_reduction_type_can_be_salvaged_from_the_note_text():
    note = "Solar output will drop to about 20% from 1 PM to 3 PM."
    result = assemble(primitive(directive_type="solar_reduction", window=WINDOW_13_15,
                                reduction_type=None, percent=20), note)
    assert result["directive_type"] == "solar_reduction"
    assert result["structured_adjustment"] == {"hours": [13, 14], "factor": 0.2}


@pytest.mark.parametrize("value", [None, {}, {"number": None, "unit": "kwh"}, {"number": "many", "unit": "kwh"},
                                   {"number": -50, "unit": "kwh"}])
def test_reserve_without_a_usable_number_is_not_invented(value):
    result = assemble(primitive(directive_type="minimum_battery_reserve",
                                window={"start_hour": 18, "end_hour": 21}, value=value),
                      "The battery should hold some energy back from 6 PM to 9 PM.")
    assert result["directive_type"] == "no_op"


def test_reserve_above_capacity_is_rejected_not_clamped():
    result = assemble(primitive(directive_type="minimum_battery_reserve",
                                window={"start_hour": 18, "end_hour": 21},
                                value={"number": 999, "unit": "kwh"}),
                      "Keep at least 999 kWh in the battery from 6 PM to 9 PM.", cap=200.0)
    assert result["directive_type"] == "no_op"
    assert result["structured_adjustment"] is None


def test_reserve_equal_to_capacity_is_accepted():
    result = assemble(primitive(directive_type="minimum_battery_reserve",
                                window={"start_hour": 18, "end_hour": 21},
                                value={"number": 200, "unit": "kwh"}),
                      "Keep the battery full from 6 PM to 9 PM.", cap=200.0)
    assert result["structured_adjustment"] == {"hours": [18, 19, 20], "minimum_energy_kwh": 200.0}


@pytest.mark.parametrize("value", [None, {}, {"number": None, "unit": "kwh"}, {"number": "x", "unit": "kwh"},
                                   {"number": -10, "unit": "kwh"}, {"number": float("inf"), "unit": "kwh"}])
def test_grid_cap_without_a_usable_number_does_not_become_zero(value):
    result = assemble(primitive(directive_type="max_grid_window",
                                window={"start_hour": 17, "end_hour": 19}, value=value),
                      "Grid import will be curtailed between 5 PM and 7 PM.")
    assert result["directive_type"] == "no_op"


def test_grid_cap_of_zero_written_by_the_operator_is_kept():
    result = assemble(primitive(directive_type="max_grid_window", window={"start_hour": 17, "end_hour": 19},
                                value={"number": 0, "unit": "kwh"}),
                      "No grid import at all between 5 PM and 7 PM.")
    assert result["structured_adjustment"] == {"hours": [17, 18], "max_grid_kwh": 0.0}


def test_duplicate_note_index_keeps_the_first_entry():
    answer = json.dumps({"interpretations": [
        primitive(0, "no_op"),
        primitive(0, "no_charge_window", window={"start_hour": 14, "end_hour": 16}),
    ]})
    with patch.object(llm, "get_llm_client", return_value=(fake_client(content=answer), "mock")):
        out = json.loads(llm.call_llm_for_interpretations(NOTE, 200.0))
    assert out["interpretations"][0]["directive_type"] == "no_op"


# --- 4. Fallback parser must not invent values either -------------------------------------------

@pytest.mark.parametrize("note", [
    "Rooftop solar output will be reduced from 1 PM to 3 PM.",
    "The solar panels will be cleaned from 1 PM to 3 PM.",
])
def test_fallback_solar_without_a_stated_amount_is_no_op(note):
    result = parse_operator_note_fallback(note, 0, 200.0)
    assert result["directive_type"] == "no_op"


def test_fallback_reserve_above_capacity_is_no_op():
    result = parse_operator_note_fallback("Keep at least 999 kWh in the battery from 6 PM to 9 PM.", 0, 200.0)
    assert result["directive_type"] == "no_op"


def test_fallback_still_parses_stated_amounts():
    solar = parse_operator_note_fallback("Solar output will drop to about 20% from 1 PM to 3 PM.", 0, 200.0)
    assert solar["structured_adjustment"] == {"hours": [13, 14], "factor": 0.2}
    reserve = parse_operator_note_fallback("Keep at least 120 kWh in reserve from 6 PM until 9 PM.", 0, 200.0)
    assert reserve["structured_adjustment"] == {"hours": [18, 19, 20], "minimum_energy_kwh": 120.0}


@pytest.mark.parametrize("note,factor", [
    ("Solar output will drop to about 25% from 1 PM to 3 PM.", 0.25),
    ("Solar output drops to approximately 40% from 1 PM to 3 PM.", 0.4),
    ("Rooftop PV output falls to only 10% from 1 PM to 3 PM.", 0.1),
    ("Solar output will drop to 20% from 1 PM to 3 PM.", 0.2),
])
def test_fallback_reads_the_stated_percent_not_a_default(note, factor):
    result = parse_operator_note_fallback(note, 0, 200.0)
    assert result["structured_adjustment"] == {"hours": [13, 14], "factor": factor}


@pytest.mark.parametrize("note,hours,factor", [
    ("Haze is expected to halve rooftop PV generation from 10 AM to 12 PM.", [10, 11], 0.5),
    ("Rooftop PV output will be halved from 10 AM to 12 PM.", [10, 11], 0.5),
    ("A dust storm will cut solar production by 60 percent from 9 AM until noon.", [9, 10, 11], 0.4),
    ("Expect a reduction of 30 percent in solar output from 9 AM until noon.", [9, 10, 11], 0.7),
    ("Solar production will fall by 12.5% from 9 AM until noon.", [9, 10, 11], 0.875),
    ("Only three quarters of the forecast solar will be usable between 2 PM and 5 PM.", [14, 15, 16], 0.75),
    ("Cloud cover will leave roughly one-fifth of the normal solar output from 1 PM to 3 PM.", [13, 14], 0.2),
    ("Solar generation will be reduced by a quarter from 1 PM to 3 PM.", [13, 14], 0.75),
    ("Only 30 percent of the forecast solar will be usable between 2 PM and 5 PM.", [14, 15, 16], 0.3),
])
def test_fallback_reads_common_solar_wordings(note, hours, factor):
    result = parse_operator_note_fallback(note, 0, 200.0)
    assert result["directive_type"] == "solar_reduction", result
    assert result["structured_adjustment"]["hours"] == hours
    assert result["structured_adjustment"]["factor"] == pytest.approx(factor, abs=1e-4)


@pytest.mark.parametrize("note", [
    "The battery state of charge must not fall below 40% of capacity from 5 PM to 8 PM.",
    "Keep at least 120 kWh in reserve from 6 PM until 9 PM.",
    "Grid import must not exceed 80 kWh between 5 PM and 9 PM.",
])
def test_solar_wording_does_not_hijack_other_directives(note):
    assert parse_operator_note_fallback(note, 0, 500.0)["directive_type"] != "solar_reduction"


@pytest.mark.parametrize("note,hours,factor", [
    ("Heavy dust storm expected from 1 PM to 3 PM; expect 80% solar reduction.", [13, 14], 0.2),
    ("Expect an 80 percent rooftop solar reduction from 1 PM to 3 PM.", [13, 14], 0.2),
    ("A 50% PV output cut is planned from 9 AM to 11 AM.", [9, 10], 0.5),
])
def test_fallback_reads_percent_followed_by_words_then_reduction(note, hours, factor):
    result = parse_operator_note_fallback(note, 0, 200.0)
    assert result["directive_type"] == "solar_reduction", result
    assert result["structured_adjustment"] == {"hours": hours, "factor": pytest.approx(factor, abs=1e-4)}


@pytest.mark.parametrize("text,hours", [
    ("during the 1-3 PM maintenance window", [13, 14]),
    ("from 1 to 3 PM", [13, 14]),
    ("between 8-10 PM tonight", [20, 21]),
    ("from 10-2 PM", [10, 11, 12, 13]),
    ("from 11-1 PM", [11, 12]),
    ("from 9-11 AM", [9, 10]),
    ("from 1 PM to 3 PM", [13, 14]),
    ("from 13:00 to 15:00", [13, 14]),
    ("from 10 PM to 2 AM", [0, 1, 22, 23]),
])
def test_fallback_time_windows_share_a_trailing_am_pm(text, hours):
    from app.fallback_parser import extract_hours_window
    assert extract_hours_window(text) == hours
