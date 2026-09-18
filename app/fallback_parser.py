"""Deterministic regex-based fallback parser for GridWise operator notes.

Used as the final deterministic safety net when LLM calls fail or cannot be validated.
Never crashes, never invents directives, degrades safely to no_op.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("gridwise.fallback_parser")


_WORD_TO_NUM = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}


def _parse_time_anchor(text: str, default_meridiem: Optional[str] = None) -> Optional[int]:
    """Parses single time anchors like 'noon', 'midnight', '3 PM', '14:00', 'one'."""
    text = text.strip().lower()
    if text == "noon":
        return 12
    if text in ("midnight", "12 am", "12:00 am"):
        return 0
    if text in ("12 pm", "12:00 pm"):
        return 12

    if text in _WORD_TO_NUM:
        hr = _WORD_TO_NUM[text]
        if default_meridiem == "pm" and hr < 12:
            hr += 12
        return hr

    # Match '3 PM' or '3:00 PM' or '15:00' or '15'
    m_12h = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$", text)
    if m_12h:
        hr = int(m_12h.group(1))
        meridiem = m_12h.group(3)
        if meridiem == "pm" and hr != 12:
            hr += 12
        elif meridiem == "am" and hr == 12:
            hr = 0
        return hr if 0 <= hr <= 23 else None

    m_24h = re.match(r"^(\d{1,2})(?::(\d{2}))?$", text)
    if m_24h:
        hr = int(m_24h.group(1))
        if default_meridiem == "pm" and hr < 12:
            hr += 12
        return hr if 0 <= hr <= 23 else None

    return None


_BARE_HOUR = re.compile(r"\d{1,2}(?::\d{2})?")
_MERIDIEM_END = re.compile(r"(am|pm)$")


def _share_meridiem(start: str, end: str) -> Tuple[str, str]:
    """Gives a bare hour the am/pm of the other end of the range, e.g. '1-3 PM' -> ('1 pm', '3 pm').

    Both am/pm readings are tried and the shorter window wins, so '11 to 3 PM' is 11 AM-3 PM and
    '10 to 2 AM' is 10 PM-2 AM. Ranges where both ends are bare (24-hour style) are left alone.
    """
    start, end = start.strip(), end.strip()
    for bare, other, bare_is_start in ((start, end, True), (end, start, False)):
        given = _MERIDIEM_END.search(other)
        if not (_BARE_HOUR.fullmatch(bare) and given):
            continue
        same = given.group(1)
        best: Optional[Tuple[int, str]] = None
        for meridiem in (same, "am" if same == "pm" else "pm"):
            candidate = f"{bare} {meridiem}"
            s = _parse_time_anchor(candidate if bare_is_start else other)
            e = _parse_time_anchor(other if bare_is_start else candidate)
            if s is None or e is None:
                continue
            length = (e - s) % 24 or 24
            if best is None or length < best[0]:
                best = (length, candidate)
        if best:
            return (best[1], end) if bare_is_start else (start, best[1])
    return start, end


def extract_hours_window(text: str) -> Optional[List[int]]:
    """Extracts half-open interval [start, end) hours as a sorted list of unique ints 0..23."""
    lower = text.lower()

    # Special phrases
    if "all day" in lower or "entire day" in lower or "whole day" in lower:
        return list(range(24))

    # "after 8 PM" / "after 20:00"
    m_after = re.search(r"after\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?|noon|midnight)", lower)
    if m_after:
        start = _parse_time_anchor(m_after.group(1))
        if start is not None:
            return list(range(start, 24))

    # "rest of the day"
    if "rest of the day" in lower:
        m_start = re.search(r"(?:from|starting at|at)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?|noon|midnight)", lower)
        if m_start:
            start = _parse_time_anchor(m_start.group(1))
            if start is not None:
                return list(range(start, 24))

    # Spoken time range: "from one until three in the afternoon"
    m_spoken = re.search(r"(?:from\s+)?(\w+)\s+(?:to|until|-)\s+(\w+)\s+in\s+the\s+(afternoon|evening|morning)", lower)
    if m_spoken:
        s_w, e_w, tod = m_spoken.group(1), m_spoken.group(2), m_spoken.group(3)
        meridiem = "pm" if tod in ("afternoon", "evening") else "am"
        s = _parse_time_anchor(s_w, meridiem)
        e = _parse_time_anchor(e_w, meridiem)
        if s is not None and e is not None and s < e:
            return list(range(s, e))

    # Standard range patterns:
    # "from noon until 2 PM", "between 1 PM and 3 PM", "13:00 to 15:00", "1 PM to 3 PM", "10 PM to 2 AM"
    patterns = [
        r"(?:from|between)?\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?|noon|midnight)\s*(?:to|until|-|and)\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?|noon|midnight)",
        r"(\d{1,2})\s*(?:am|pm)?\s*(?:-|to|until)\s*(\d{1,2})\s*(am|pm)",
    ]

    for pat in patterns:
        m = re.search(pat, lower)
        if m:
            s_str, e_str = m.group(1), m.group(2)
            # If pattern 2 matched with meridiem at end (e.g. 1 to 3 PM)
            if len(m.groups()) == 3 and m.group(3):
                meridiem = m.group(3)
                if not ("am" in s_str or "pm" in s_str or s_str in ("noon", "midnight")):
                    s_str = f"{s_str} {meridiem}"
                e_str = f"{e_str} {meridiem}"

            s_str, e_str = _share_meridiem(s_str, e_str)
            s = _parse_time_anchor(s_str)
            e = _parse_time_anchor(e_str)

            # Special case: midnight as end anchor is hour 24
            if e_str.strip() in ("midnight", "24", "24:00"):
                e = 24

            if s is not None and e is not None:
                if s == e:
                    return [s]
                elif s < e:
                    return list(range(s, e))
                else:
                    # Midnight wrap-around e.g. 10 PM to 2 AM -> [22, 23, 0, 1]
                    return sorted(list(range(s, 24)) + list(range(0, e)))

    # Single hour match: "at 3 PM", "during hour 15", "at noon"
    m_single = re.search(r"(?:at|during hour)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?|noon)", lower)
    if m_single:
        hr = _parse_time_anchor(m_single.group(1))
        if hr is not None and hr < 24:
            return [hr]

    return None


_PCT = r"(\d+(?:\.\d+)?)\s*(?:%|percent|per cent)"
_LEADIN = r"(?:(?:about|around|approximately|roughly|nearly|only|just)\W+)?"
_REDUCTION_VERB = r"(?:reduc\w*|cut|drop\w*|decreas\w*|lower\w*|fall\w*|los[et]\w*)"
_FRACTIONS = {
    "three quarters": 0.75, "three-quarters": 0.75, "three fourths": 0.75,
    "two thirds": 2 / 3, "two-thirds": 2 / 3,
    "one quarter": 0.25, "a quarter": 0.25, "one fourth": 0.25, "one-fourth": 0.25,
    "one third": 1 / 3, "a third": 1 / 3, "one-third": 1 / 3,
    "one fifth": 0.2, "one-fifth": 0.2, "a fifth": 0.2,
    "one tenth": 0.1, "one-tenth": 0.1, "a tenth": 0.1,
}


def _solar_factor(lower: str) -> Optional[float]:
    """Usable fraction of solar left (0..1) from a solar note, or None when no amount is stated.

    Never invents an amount: an unreadable note returns None and stays no_op.
    """
    if "offline" in lower or "no solar" in lower or "zero solar" in lower:
        return 0.0
    if "halve" in lower or "half" in lower:
        return 0.5

    # "drop to about 20%", "roughly 25% of the forecast", "only 30 percent usable" -> amount that remains
    m = re.search(
        r"(?:roughly|treated as|remains?|keeps?|drops? to|falls? to|reduced to|down to|leaves?|leaving|only)\W+"
        + _LEADIN + _PCT,
        lower,
    )
    if m:
        return max(0.0, min(1.0, float(m.group(1)) / 100.0))

    # "cut by 60 percent", "reduction of 30%", "80% reduction" -> amount removed
    m = re.search(_REDUCTION_VERB + r"\b(?:\W+\w+){0,5}?\W+(?:by|of)\W+" + _LEADIN + _PCT, lower)
    if not m:
        m = re.search(_PCT + r"\s*(?:\w+\s+){0,2}?(?:haze|cloud|reduction|cut|drop|decrease|loss)", lower)
    if not m:
        m = re.search(r"(?:reduction|decrease|drop|loss)\s+of\W+" + _LEADIN + _PCT, lower)
    if m:
        return max(0.0, min(1.0, 1.0 - float(m.group(1)) / 100.0))

    # Spoken fractions: "three quarters ... usable" remains, "reduced by a quarter" is removed
    for phrase in sorted(_FRACTIONS, key=len, reverse=True):
        found = re.search(r"\b" + re.escape(phrase) + r"\b", lower)
        if found:
            fraction = _FRACTIONS[phrase]
            removed = re.search(_REDUCTION_VERB + r"\b(?:\W+\w+){0,5}?\W+(?:by|of)\W+" + _LEADIN + r"$", lower[: found.start()])
            return 1.0 - fraction if removed else fraction

    return None


def parse_operator_note_fallback(
    note_text: str,
    note_index: int,
    battery_capacity_kwh: Optional[float] = None,
) -> Dict[str, Any]:
    """Parses a single operator note using deterministic heuristics and regex.

    Returns a dict conforming to DirectiveInterpretation schema.
    """
    text = note_text.strip()
    lower = text.lower()
    cap = battery_capacity_kwh or 500.0  # Safe default capacity

    # 0. Historical / informational notes
    if ("yesterday" in lower or "performed at 100%" in lower or "100% efficiency" in lower) and not any(w in lower for w in ["expect", "will", "tomorrow"]):
        return {
            "note_index": note_index,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": f"Fallback rule: normal or historical note: {text[:60]}...",
        }

    hours = extract_hours_window(text)

    # 1. Solar Reduction
    solar_keywords = ["solar", "panel", "panels", "pv", "sun", "rooftop"]
    has_solar = any(k in lower for k in solar_keywords)

    if has_solar and hours and any(w in lower for w in ["reduc", "curtail", "cut", "drop", "fall", "wash", "clean", "offline", "forecast", "haze", "cloud", "storm", "dust", "halve", "half"]):
        factor = _solar_factor(lower)

        if factor is not None:
            return {
                "note_index": note_index,
                "applies": True,
                "directive_type": "solar_reduction",
                "structured_adjustment": {"hours": hours, "factor": round(factor, 4)},
                "explanation": f"Fallback rule: parsed solar reduction to factor {factor:.2f} during hours {hours}",
            }

    # 2. No Discharge Window (Check before charge window to avoid substring collisions)
    discharge_keywords = [
        "discharge", "discharging", "draw from storage", "draw power from the battery",
        "draw power from battery", "freeze battery", "drain", "preserve battery",
    ]
    has_discharge = any(k in lower for k in discharge_keywords)

    if (has_discharge or ("freeze" in lower and "battery" in lower)) and hours:
        return {
            "note_index": note_index,
            "applies": True,
            "directive_type": "no_discharge_window",
            "structured_adjustment": {"hours": hours},
            "explanation": f"Fallback rule: parsed no-discharge window during hours {hours}",
        }

    # 3. No Charge Window
    charge_keywords = ["charge", "charging", "charger"]
    negation_keywords = [
        "no ", "don't", "do not", "stop", "prevent", "forbid", "forbidden", "prohibit",
        "prohibited", "disable", "disabled", "maintenance", "freeze", "unavailable",
        "isolated", "offline", "avoid",
    ]
    has_charge = any(k in lower for k in charge_keywords) and not any(d in lower for d in ["discharge", "discharging", "draw power"])
    has_neg = any(k in lower for k in negation_keywords)

    if has_charge and has_neg and hours:
        return {
            "note_index": note_index,
            "applies": True,
            "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": hours},
            "explanation": f"Fallback rule: parsed no-charge window during hours {hours}",
        }

    # 4. Minimum Battery Reserve
    reserve_keywords = [
        "reserve", "minimum", "at least", "no lower than", "maintain", "keep >= ",
        "keep at or above", "hold", "remain in the battery", "drop below", "not drop below",
    ]
    has_battery = any(k in lower for k in ["battery", "storage", "soc", "charge level", "energy"])
    has_reserve = any(k in lower for k in reserve_keywords)

    if (has_reserve or "reserve" in lower) and (has_battery or "kwh" in lower or "mwh" in lower) and hours:
        # Extract amount
        min_kwh = None
        # Check percentage of capacity: "50% of capacity", "half the battery"
        if "half" in lower:
            min_kwh = cap * 0.5
        m_pct = re.search(r"(\d+)\s*%\s*(?:of\s*(?:the\s*)?(?:battery|capacity))?", lower)
        if m_pct and "capacity" in lower:
            min_kwh = cap * (float(m_pct.group(1)) / 100.0)

        # Check numeric kWh or MWh
        if min_kwh is None:
            m_val = re.search(r"(\d+(?:\.\d+)?)\s*(kwh|mwh)", lower)
            if m_val:
                val = float(m_val.group(1))
                unit = m_val.group(2)
                min_kwh = val * 1000.0 if unit == "mwh" else val

        # A reserve above battery capacity is invalid (Problem Statement section 8): reject, never clamp
        if min_kwh is not None and min_kwh <= cap + 1e-9:
            return {
                "note_index": note_index,
                "applies": True,
                "directive_type": "minimum_battery_reserve",
                "structured_adjustment": {"hours": hours, "minimum_energy_kwh": round(min_kwh, 2)},
                "explanation": f"Fallback rule: parsed minimum battery reserve {min_kwh:.2f} kWh during hours {hours}",
            }

    # 5. Max Grid Window
    has_grid = any(k in lower for k in ["grid", "import", "utility", "intake", "transformer", "feeder", "substation"])
    has_limit = any(k in lower for k in ["cap", "limit", "max", "no more than", "exceed", "below", "at or below", "restriction", "constrained", "restricted", "draw", "take more than"])

    if (has_grid and has_limit) and hours:
        m_grid = re.search(r"(?:cap|limit|max|restricted to at most|take more than|draw|import|draw power)\s+(?:(?:grid|power|import)\s+)*(?:at\s+|to\s+)?(\d+(?:\.\d+)?)\s*(?:kwh|kw)?", lower)
        if not m_grid:
            m_grid = re.search(r"(\d+(?:\.\d+)?)\s*(?:kwh|kw)", lower)

        if m_grid:
            max_kwh = float(m_grid.group(1))
            return {
                "note_index": note_index,
                "applies": True,
                "directive_type": "max_grid_window",
                "structured_adjustment": {"hours": hours, "max_grid_kwh": round(max_kwh, 2)},
                "explanation": f"Fallback rule: parsed max grid import limit {max_kwh:.2f} kWh during hours {hours}",
            }

    # 6. Default to safe no_op
    return {
        "note_index": note_index,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": f"Fallback rule: unclassified or informational note: {text[:60]}...",
    }
