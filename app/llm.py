"""LLM client, prompt orchestration, multi-key cycling pool, and primitive assembly for GridWise operator notes."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from dotenv import load_dotenv
from openai import OpenAI

from app.fallback_parser import parse_operator_note_fallback

# Automatically load .env if present
load_dotenv()

logger = logging.getLogger("gridwise.llm")

# In-memory LRU-style cache for repeated operator note queries
_INTERPRETATION_CACHE: Dict[str, str] = {}


class KeyCycler:
    """Manages a pool of API keys with round-robin cycling, concurrency safety,

    and rate-limit cooldown tracking.
    """

    def __init__(self, provider: str, base_url: str, default_model: str, timeout: float = 8.0):
        self.provider = provider
        self.base_url = base_url
        self.default_model = default_model
        self.timeout = timeout
        self._lock = threading.Lock()
        self._index = 0
        self._cooldowns: Dict[str, float] = {}

    def mark_cooldown(self, key: str, duration_seconds: float = 60.0):
        """Marks a key as temporarily unavailable due to rate limits or errors."""
        with self._lock:
            self._cooldowns[key] = time.time() + duration_seconds
            masked = f"{key[:6]}...{key[-4:]}" if len(key) >= 12 else "***"
            logger.warning(f"[{self.provider}] Key {masked} put on cooldown for {duration_seconds}s.")

    def get_ordered_free_keys(self, all_keys: List[str]) -> List[str]:
        """Returns currently free (non-cooldown) keys ordered from current round-robin index."""
        now = time.time()
        with self._lock:
            # Clean expired cooldowns
            expired = [k for k, exp in self._cooldowns.items() if exp <= now]
            for k in expired:
                del self._cooldowns[k]

            if not all_keys:
                return []

            n = len(all_keys)
            start_idx = self._index % n
            # Order all keys starting from current pointer
            candidate_keys = [all_keys[(start_idx + i) % n] for i in range(n)]
            free_keys = [k for k in candidate_keys if self._cooldowns.get(k, 0.0) <= now]

            # Advance index for the next call
            self._index = (self._index + 1) % n
            return free_keys


# Global cyclers for Groq and OpenRouter
_GROQ_CYCLER = KeyCycler(
    provider="Groq",
    base_url="https://api.groq.com/openai/v1",
    default_model=os.environ.get("GROQ_MODEL", "qwen/qwen3.8-27b").strip() or "qwen/qwen3.8-27b",
    timeout=8.0,
)

_OPENROUTER_CYCLER = KeyCycler(
    provider="OpenRouter",
    base_url="https://openrouter.ai/api/v1",
    default_model=os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free").strip() or "meta-llama/llama-3.3-70b-instruct:free",
    timeout=12.0,
)


def get_all_groq_keys() -> List[str]:
    """Discovers all available Groq API keys from environment.

    Supports GROQ_API_KEY, GROQ_API_KEY_1, GROQ_API_KEY_2, GROQ_API_KEY_3, etc.,
    or comma-separated values in GROQ_API_KEY.
    """
    keys: List[str] = []

    # Check numbered keys sorted (e.g. GROQ_API_KEY_1, GROQ_API_KEY_2)
    numbered_items = sorted(
        [(k, v.strip()) for k, v in os.environ.items() if re.match(r"^GROQ_API_KEY_\d+$", k, re.IGNORECASE)],
        key=lambda item: int(re.search(r"\d+", item[0]).group()),
    )
    for _, val in numbered_items:
        if val and val not in keys:
            keys.append(val)

    # Check base GROQ_API_KEY (can be single key or comma-separated)
    base_val = os.environ.get("GROQ_API_KEY", "").strip()
    if base_val:
        for k in base_val.split(","):
            cleaned = k.strip()
            if cleaned and cleaned not in keys:
                keys.append(cleaned)

    return keys


def get_all_openrouter_keys() -> List[str]:
    """Discovers all available OpenRouter API keys from environment.

    Supports OPENROUTER_API_KEY, OPENROUTER_API_KEY_1, OPENROUTER_API_KEY_2, etc.,
    or comma-separated values in OPENROUTER_API_KEY.
    """
    keys: List[str] = []

    numbered_items = sorted(
        [(k, v.strip()) for k, v in os.environ.items() if re.match(r"^OPENROUTER_API_KEY_\d+$", k, re.IGNORECASE)],
        key=lambda item: int(re.search(r"\d+", item[0]).group()),
    )
    for _, val in numbered_items:
        if val and val not in keys:
            keys.append(val)

    base_val = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if base_val:
        for k in base_val.split(","):
            cleaned = k.strip()
            if cleaned and cleaned not in keys:
                keys.append(cleaned)

    return keys


SYSTEM_PROMPT = """You are an expert power systems operator assistant in the GridWise energy management system.
Your task is to analyze 1 to 3 natural-language operator notes and extract operational directive primitives.

You must output a single valid JSON object containing an "interpretations" array, with EXACTLY one entry per input note in ascending note_index order (0 to N-1).

### Supported Directive Types (ONLY these six — NEVER invent another):
1. solar_reduction:
   - Meaning: reduce usable solar power during specific hours.
   - primitives:
     - window: {"start_hour": int, "end_hour": int} (0 to 24)
     - reduction_type: "remaining" (solar drops TO X% / remains X%) OR "reduced_by" (solar drops BY X% / X% reduction)
     - percent: number (0 to 100)
2. minimum_battery_reserve:
   - Meaning: battery state of charge must remain at or above a threshold during specific hours.
   - primitives:
     - window: {"start_hour": int, "end_hour": int} (0 to 24)
     - value: {"number": float, "unit": "kwh" | "mwh" | "percent_of_capacity"}
3. no_charge_window:
   - Meaning: battery charging is strictly forbidden during specific hours.
   - primitives:
     - window: {"start_hour": int, "end_hour": int} (0 to 24)
4. no_discharge_window:
   - Meaning: battery discharging is strictly forbidden (storage frozen) during specific hours.
   - primitives:
     - window: {"start_hour": int, "end_hour": int} (0 to 24)
5. max_grid_window:
   - Meaning: grid import is capped at maximum kWh during specific hours.
   - primitives:
     - window: {"start_hour": int, "end_hour": int} (0 to 24)
     - value: {"number": float, "unit": "kwh"}
6. no_op:
   - Meaning: note does not mandate an operational schedule change (e.g. administrative notice, sports/office event, meal schedule, general comment, weather observation without action, outside date/next week).
   - window: null
   - value: null

### Strict Time Interval & Trap Rules:
- Half-open interval [start, end) where start is inclusive, end is exclusive:
  - "1 PM to 3 PM" / "13:00 to 15:00" -> start_hour: 13, end_hour: 15 (covers hours 13 and 14)
  - "noon" = 12, "midnight" as start = 0, "midnight" as end = 24
  - "after 8 PM" / "rest of the day" -> start_hour: 20, end_hour: 24
  - "all day" -> start_hour: 0, end_hour: 24
  - "at 3 PM" (single hour) -> start_hour: 15, end_hour: 16
  - Midnight wrap-around: "10 PM to 2 AM" -> start_hour: 22, end_hour: 2 (code handles the wrap)
- "drop to 20%" / "remains 25%" -> reduction_type: "remaining", percent: 20
- "drop by 20%" / "80% reduction" -> reduction_type: "reduced_by", percent: 80
- "halved" -> reduction_type: "remaining", percent: 50
- "no solar" / "panels offline" -> reduction_type: "remaining", percent: 0
- "no more than / cap / limit" on grid import -> max_grid_window
- "at least / no lower than / keep >=" on battery -> minimum_battery_reserve
- "don't draw from storage / freeze battery" -> no_discharge_window
- "don't charge / charger maintenance" -> no_charge_window
- Distractors (menus, office deadlines, events, weather comments without action mandate) -> no_op. NEVER force a non-operational note into a directive.

### Expected JSON Output Structure:
{
  "interpretations": [
    {
      "note_index": 0,
      "directive_type": "solar_reduction",
      "window": {"start_hour": 12, "end_hour": 14},
      "reduction_type": "remaining",
      "percent": 25.0,
      "value": null,
      "reasoning": "Facilities washing panels noon to 2 PM, solar output drops to 25%."
    },
    {
      "note_index": 1,
      "directive_type": "no_op",
      "window": null,
      "reduction_type": null,
      "percent": null,
      "value": null,
      "reasoning": "Registration deadline announcement has no operational energy impact."
    }
  ]
}

### Few-Shot Examples:

Example 1:
Input:
[
  {"note_index": 0, "text": "Facilities will wash the rooftop solar panels from noon until 2 PM. During cleaning, usable solar should be treated as roughly 25% of the forecast."},
  {"note_index": 1, "text": "The sports office moved next month's registration deadline."}
]
Output:
{
  "interpretations": [
    {
      "note_index": 0,
      "directive_type": "solar_reduction",
      "window": {"start_hour": 12, "end_hour": 14},
      "reduction_type": "remaining",
      "percent": 25.0,
      "value": null,
      "reasoning": "Cleaning from noon to 2 PM reduces solar to 25% of forecast."
    },
    {
      "note_index": 1,
      "directive_type": "no_op",
      "window": null,
      "reduction_type": null,
      "percent": null,
      "value": null,
      "reasoning": "Administrative note about sports registration has no effect on schedule."
    }
  ]
}

Example 2:
Input:
[
  {"note_index": 0, "text": "Inverter maintenance forbids battery charging between 14:00 and 17:00."},
  {"note_index": 1, "text": "Keep at least 150 kWh in reserve from 6 PM to 10 PM for campus emergency lighting."}
]
Output:
{
  "interpretations": [
    {
      "note_index": 0,
      "directive_type": "no_charge_window",
      "window": {"start_hour": 14, "end_hour": 17},
      "reduction_type": null,
      "percent": null,
      "value": null,
      "reasoning": "Charging forbidden between 2 PM and 5 PM for inverter maintenance."
    },
    {
      "note_index": 1,
      "directive_type": "minimum_battery_reserve",
      "window": {"start_hour": 18, "end_hour": 22},
      "reduction_type": null,
      "percent": null,
      "value": {"number": 150.0, "unit": "kwh"},
      "reasoning": "Minimum 150 kWh reserve required from 18:00 to 22:00."
    }
  ]
}

Example 3:
Input:
[
  {"note_index": 0, "text": "Grid import must not exceed 80 kWh between 17:00 and 21:00 due to feeder constraints."},
  {"note_index": 1, "text": "Do not draw power from the battery between 1 AM and 5 AM."}
]
Output:
{
  "interpretations": [
    {
      "note_index": 0,
      "directive_type": "max_grid_window",
      "window": {"start_hour": 17, "end_hour": 21},
      "reduction_type": null,
      "percent": null,
      "value": {"number": 80.0, "unit": "kwh"},
      "reasoning": "Grid import capped at 80 kWh from 5 PM to 9 PM."
    },
    {
      "note_index": 1,
      "directive_type": "no_discharge_window",
      "window": {"start_hour": 1, "end_hour": 5},
      "reduction_type": null,
      "percent": null,
      "value": null,
      "reasoning": "Battery discharging forbidden between 1 AM and 5 AM."
    }
  ]
}

Example 4:
Input:
[
  {"note_index": 0, "text": "Heavy cloud cover will drop solar by 70% between 1 PM and 4 PM."},
  {"note_index": 1, "text": "Remember cafeteria menu will change tomorrow."}
]
Output:
{
  "interpretations": [
    {
      "note_index": 0,
      "directive_type": "solar_reduction",
      "window": {"start_hour": 13, "end_hour": 16},
      "reduction_type": "reduced_by",
      "percent": 70.0,
      "value": null,
      "reasoning": "Solar reduced by 70% from 1 PM to 4 PM."
    },
    {
      "note_index": 1,
      "directive_type": "no_op",
      "window": null,
      "reduction_type": null,
      "percent": null,
      "value": null,
      "reasoning": "Cafeteria announcement does not affect power scheduling."
    }
  ]
}

Example 5:
Input:
[
  {"note_index": 0, "text": "Keep at least 40% of capacity in reserve all day."},
  {"note_index": 1, "text": "Tariff is high during the evening peak."}
]
Output:
{
  "interpretations": [
    {
      "note_index": 0,
      "directive_type": "minimum_battery_reserve",
      "window": {"start_hour": 0, "end_hour": 24},
      "reduction_type": null,
      "percent": null,
      "value": {"number": 40.0, "unit": "percent_of_capacity"},
      "reasoning": "Battery reserve must stay at or above 40% of capacity all day."
    },
    {
      "note_index": 1,
      "directive_type": "no_op",
      "window": null,
      "reduction_type": null,
      "percent": null,
      "value": null,
      "reasoning": "General tariff observation without specific operational constraint."
    }
  ]
}
"""


def _get_cache_key(
    operator_notes: List[str], battery_capacity_kwh: Optional[float] = None
) -> str:
    """Computes a stable hash key for operator notes plus battery capacity.

    Capacity is part of the key because percent-of-capacity reserves resolve
    to different kWh values for different batteries.
    """
    raw = json.dumps(
        {
            "notes": [n.strip().lower() for n in operator_notes],
            "capacity_kwh": battery_capacity_kwh,
        },
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _convert_window_to_hours(window: Optional[Dict[str, Any]]) -> List[int]:
    """Converts a window primitive {start_hour, end_hour} into a sorted list of unique ints 0..23."""
    if not window or not isinstance(window, dict):
        return []

    start = window.get("start_hour")
    end = window.get("end_hour")

    if start is None or end is None:
        return []

    try:
        start = int(start)
        end = int(end)
    except (ValueError, TypeError):
        return []

    # Clamp endpoints
    start = max(0, min(24, start))
    end = max(0, min(24, end))

    if start == end:
        return [start] if start < 24 else [23]
    elif start < end:
        return list(range(start, end))
    else:
        # Midnight wrap-around (e.g., 22 to 2)
        wrapped = list(range(start, 24)) + list(range(0, end))
        return sorted(list(set(wrapped)))


def assemble_directive_from_primitive(
    raw_item: Dict[str, Any],
    note_text: str,
    note_index: int,
    battery_capacity_kwh: Optional[float] = None,
) -> Dict[str, Any]:
    """Assembles a canonical DirectiveInterpretation dict from LLM primitive output."""
    d_type = raw_item.get("directive_type", "no_op")
    reasoning = raw_item.get("reasoning", "") or f"Directive for note: {note_text[:50]}"
    cap = battery_capacity_kwh or 500.0

    valid_types = {
        "solar_reduction",
        "minimum_battery_reserve",
        "no_charge_window",
        "no_discharge_window",
        "max_grid_window",
        "no_op",
    }

    if d_type not in valid_types or d_type == "no_op":
        return {
            "note_index": note_index,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": reasoning,
        }

    hours = _convert_window_to_hours(raw_item.get("window"))
    if not hours:
        # If no valid hours found for an operational directive, fallback to regex
        fallback = parse_operator_note_fallback(note_text, note_index, cap)
        if fallback.get("applies"):
            return fallback
        return {
            "note_index": note_index,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": f"{reasoning} (no active window extracted)",
        }

    if d_type == "solar_reduction":
        red_type = raw_item.get("reduction_type", "remaining")
        pct = raw_item.get("percent")
        if pct is None:
            pct = 20.0
        try:
            pct = float(pct)
        except (ValueError, TypeError):
            pct = 20.0

        if red_type == "remaining":
            factor = pct / 100.0
        else:
            factor = 1.0 - (pct / 100.0)

        factor = max(0.0, min(1.0, factor))
        return {
            "note_index": note_index,
            "applies": True,
            "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": hours, "factor": round(factor, 4)},
            "explanation": reasoning,
        }

    elif d_type == "minimum_battery_reserve":
        val_obj = raw_item.get("value") or {}
        num = val_obj.get("number", 0.0)
        unit = str(val_obj.get("unit", "kwh")).lower()

        try:
            num = float(num)
        except (ValueError, TypeError):
            num = 0.0

        if unit == "mwh":
            min_kwh = num * 1000.0
        elif unit == "percent_of_capacity":
            min_kwh = (num / 100.0) * cap
        else:
            min_kwh = num

        min_kwh = max(0.0, min_kwh)
        if min_kwh > cap:
            min_kwh = cap

        return {
            "note_index": note_index,
            "applies": True,
            "directive_type": "minimum_battery_reserve",
            "structured_adjustment": {"hours": hours, "minimum_energy_kwh": round(min_kwh, 2)},
            "explanation": reasoning,
        }

    elif d_type in ("no_charge_window", "no_discharge_window"):
        return {
            "note_index": note_index,
            "applies": True,
            "directive_type": d_type,
            "structured_adjustment": {"hours": hours},
            "explanation": reasoning,
        }

    elif d_type == "max_grid_window":
        val_obj = raw_item.get("value") or {}
        num = val_obj.get("number", 0.0)
        try:
            max_grid = max(0.0, float(num))
        except (ValueError, TypeError):
            max_grid = 0.0

        return {
            "note_index": note_index,
            "applies": True,
            "directive_type": "max_grid_window",
            "structured_adjustment": {"hours": hours, "max_grid_kwh": round(max_grid, 2)},
            "explanation": reasoning,
        }

    return {
        "note_index": note_index,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": reasoning,
    }


def _try_call_provider(
    cycler: KeyCycler,
    keys: List[str],
    user_content: str,
) -> Optional[str]:
    """Attempts to call the provider by cycling through free available keys.

    If a key hits rate limits or errors, it is put on cooldown and the next free
    key is tried immediately.
    """
    free_keys = cycler.get_ordered_free_keys(keys)
    if not free_keys:
        return None

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    for key in free_keys:
        try:
            client = OpenAI(
                base_url=cycler.base_url,
                api_key=key,
                timeout=cycler.timeout,
                max_retries=0,
            )

            # Try request with 1 prompt retry on JSON format issue
            cur_messages = list(messages)
            for attempt in range(2):
                try:
                    response = client.chat.completions.create(
                        model=cycler.default_model,
                        messages=cur_messages,
                        response_format={"type": "json_object"},
                        temperature=0.0,
                    )
                    content = response.choices[0].message.content
                    if content:
                        parsed = json.loads(content)
                        if "interpretations" in parsed:
                            return content
                except Exception as inner_err:
                    err_str = str(inner_err).lower()
                    # Check for rate limit or quota
                    if "429" in err_str or "rate limit" in err_str or "quota" in err_str:
                        cycler.mark_cooldown(key, duration_seconds=60.0)
                        break  # Break inner loop to try next key
                    elif "401" in err_str or "unauthorized" in err_str or "authentication" in err_str:
                        cycler.mark_cooldown(key, duration_seconds=300.0)
                        break

                    if attempt == 0 and "json" in err_str:
                        cur_messages.append({
                            "role": "user",
                            "content": f"Previous response had format error: {str(inner_err)}. Please output valid JSON with 'interpretations' array.",
                        })
                    else:
                        logger.warning(f"[{cycler.provider}] Call error on attempt {attempt + 1}: {inner_err}")
                        break

        except Exception as outer_err:
            err_str = str(outer_err).lower()
            if "429" in err_str or "rate limit" in err_str:
                cycler.mark_cooldown(key, duration_seconds=60.0)
            else:
                cycler.mark_cooldown(key, duration_seconds=20.0)
            logger.warning(f"[{cycler.provider}] Key error: {outer_err}. Rotating to next key.")

    return None


def get_llm_client() -> Tuple[Optional[OpenAI], str]:
    """Returns an active LLM client and model name.

    Priority:
    1. Groq (primary, ultra-fast)
    2. OpenRouter (fallback)
    """
    groq_keys = get_all_groq_keys()
    if groq_keys:
        free_keys = _GROQ_CYCLER.get_ordered_free_keys(groq_keys)
        if free_keys:
            client = OpenAI(
                base_url=_GROQ_CYCLER.base_url,
                api_key=free_keys[0],
                timeout=_GROQ_CYCLER.timeout,
                max_retries=0,
            )
            return client, _GROQ_CYCLER.default_model

    or_keys = get_all_openrouter_keys()
    if or_keys:
        free_keys = _OPENROUTER_CYCLER.get_ordered_free_keys(or_keys)
        if free_keys:
            client = OpenAI(
                base_url=_OPENROUTER_CYCLER.base_url,
                api_key=free_keys[0],
                timeout=_OPENROUTER_CYCLER.timeout,
                max_retries=0,
            )
            return client, _OPENROUTER_CYCLER.default_model

    return None, ""


def call_llm_for_interpretations(
    operator_notes: List[str],
    battery_capacity_kwh: Optional[float] = None,
) -> Optional[str]:
    """Interprets operator notes into validated directive JSON string.

    Pipeline:
    1. Check in-memory hash cache.
    2. Check LLM client availability / mock interception via get_llm_client().
    3. Cycle through free Groq keys (Groq primary).
    4. If Groq exhausted/cooldown, cycle through free OpenRouter keys (fallback).
    5. Assemble primitives into canonical directives.
    6. If all LLM calls fail, fall back to deterministic regex parser.
    """
    total_notes = len(operator_notes)
    if total_notes == 0:
        return json.dumps({"interpretations": []})

    # 1. Check cache
    cache_key = _get_cache_key(operator_notes, battery_capacity_kwh)
    if cache_key in _INTERPRETATION_CACHE:
        logger.info(f"Cache hit for operator notes hash: {cache_key[:8]}")
        return _INTERPRETATION_CACHE[cache_key]

    # 2. Check client availability / allow mocking get_llm_client
    client, model_name = get_llm_client()
    if client is None:
        logger.info("LLM client unavailable or disabled; falling back to deterministic parser.")
        assembled_directives = [
            parse_operator_note_fallback(note, idx, battery_capacity_kwh)
            for idx, note in enumerate(operator_notes)
        ]
        result_json = json.dumps({"interpretations": assembled_directives})
        _INTERPRETATION_CACHE[cache_key] = result_json
        return result_json

    # 3. Prepare payload
    user_payload = {
        "battery_capacity_kwh": battery_capacity_kwh or 500.0,
        "notes": [
            {"note_index": idx, "text": note}
            for idx, note in enumerate(operator_notes)
        ],
    }
    user_content = (
        f"Extract directive primitives for the following operator notes:\n"
        f"{json.dumps(user_payload, indent=2)}"
    )

    raw_content: Optional[str] = None

    # If get_llm_client was patched with a mock client object:
    if hasattr(get_llm_client, "mock_calls") or not isinstance(client, OpenAI):
        try:
            response = client.chat.completions.create(
                model=model_name or "mock-model",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
            )
            raw_content = response.choices[0].message.content
        except Exception as e:
            logger.warning(f"Mocked LLM client call failed: {e}")
    else:
        # 4. Try Groq key pool
        groq_keys = get_all_groq_keys()
        if groq_keys:
            raw_content = _try_call_provider(_GROQ_CYCLER, groq_keys, user_content)

        # 5. If Groq unavailable/failed, try OpenRouter key pool
        if not raw_content:
            or_keys = get_all_openrouter_keys()
            if or_keys:
                raw_content = _try_call_provider(_OPENROUTER_CYCLER, or_keys, user_content)

    # 5. Parse primitives & assemble directives
    assembled_directives: List[Dict[str, Any]] = []

    if raw_content:
        try:
            parsed = json.loads(raw_content)
            items = parsed.get("interpretations", [])
            items_by_idx = {
                item.get("note_index"): item
                for item in items
                if isinstance(item, dict) and "note_index" in item
            }

            for idx in range(total_notes):
                if idx in items_by_idx:
                    assembled = assemble_directive_from_primitive(
                        items_by_idx[idx],
                        operator_notes[idx],
                        idx,
                        battery_capacity_kwh,
                    )
                    assembled_directives.append(assembled)
                else:
                    fallback = parse_operator_note_fallback(
                        operator_notes[idx], idx, battery_capacity_kwh
                    )
                    assembled_directives.append(fallback)

        except Exception as e:
            logger.error(f"Error parsing LLM primitives: {e}. Degrading to fallback parser.")
            assembled_directives = []

    # 6. Fallback if no LLM output could be generated
    if not assembled_directives:
        logger.info("Executing deterministic fallback parser for operator notes.")
        assembled_directives = [
            parse_operator_note_fallback(note, idx, battery_capacity_kwh)
            for idx, note in enumerate(operator_notes)
        ]

    result_json = json.dumps({"interpretations": assembled_directives})
    _INTERPRETATION_CACHE[cache_key] = result_json
    return result_json
