"""LLM client and prompt orchestration for operator note interpretation via OpenRouter."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional
from openai import OpenAI

logger = logging.getLogger("gridwise.llm")

SYSTEM_PROMPT = """You are an expert power systems operator assistant in the GridWise energy management system.
Your sole job is to interpret 1 to 3 natural-language operator notes into structured directives for an energy optimizer.

You must output a single JSON object with the key "interpretations", containing an array of interpretation objects, exactly one per input note in ascending note_index order (0 to N-1).

### The ONLY Six Supported Directive Types:
1. solar_reduction:
   - Meaning: reduce usable solar generation during specific hours.
   - structured_adjustment: {"hours": [int, ...], "factor": float}
   - factor is the fraction of solar that REMAINS (e.g., 80% reduction means factor is 0.20; 30% curtailment means factor is 0.70).
   - factor must be in range [0.0, 1.0].
   - applies: true

2. minimum_battery_reserve:
   - Meaning: battery state of charge must remain at or above this level during specific hours.
   - structured_adjustment: {"hours": [int, ...], "minimum_energy_kwh": float}
   - minimum_energy_kwh must be non-negative.
   - applies: true

3. no_charge_window:
   - Meaning: battery charging is strictly forbidden in listed hours.
   - structured_adjustment: {"hours": [int, ...]}
   - applies: true

4. no_discharge_window:
   - Meaning: battery discharging is strictly forbidden in listed hours.
   - structured_adjustment: {"hours": [int, ...]}
   - applies: true

5. max_grid_window:
   - Meaning: grid power import is capped at a maximum kWh in listed hours.
   - structured_adjustment: {"hours": [int, ...], "max_grid_kwh": float}
   - max_grid_kwh must be non-negative.
   - applies: true

6. no_op:
   - Meaning: note does NOT affect the 24-hour schedule (e.g. general comment, weather report without action, greeting, irrelevant note).
   - structured_adjustment: null
   - applies: false

### Strict Formatting and Time Window Rules:
- Time windows are whole hours, start inclusive / end exclusive:
  - "1 PM to 3 PM" -> hours [13, 14]
  - "08:00 to 11:00" -> hours [8, 9, 10]
  - "night hours 10 PM to 6 AM" -> hours [0, 1, 2, 3, 4, 5, 22, 23] (always sorted ascending, 0-23 unique ints).
- "hours" arrays MUST be non-empty, containing unique integers between 0 and 23 in strictly ascending order.
- Every non-no_op directive MUST have applies: true.
- no_op is the ONLY directive type allowed applies: false and structured_adjustment: null.
- NEVER invent a directive type outside these six.
- NEVER invent demand, tariff, or battery limit modifications.
- DO NOT invent numbers or hours that are not stated or directly implied by the note text.
- Provide a short, concise "explanation" string for each note.

### Expected JSON Output Structure:
{
  "interpretations": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
      "explanation": "Dust storm expected between 1 PM and 3 PM reduces solar by 80%, leaving 20% factor."
    }
  ]
}
"""


def get_openrouter_client() -> Optional[OpenAI]:
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        logger.warning("OPENROUTER_API_KEY is not set.")
        return None
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
        timeout=25.0,
    )


def call_llm_for_interpretations(operator_notes: List[str]) -> Optional[str]:
    """Calls OpenRouter with operator notes and returns the raw JSON string response."""
    client = get_openrouter_client()
    if client is None:
        logger.warning("No OpenRouter client available; returning None for guardrail fallback.")
        return None

    model_name = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip() or "openai/gpt-4o-mini"

    user_payload = {
        "notes": [
            {"note_index": idx, "text": note}
            for idx, note in enumerate(operator_notes)
        ]
    }

    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Extract directives for the following operator notes:\n{json.dumps(user_payload, indent=2)}"},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        content = response.choices[0].message.content
        return content
    except Exception as e:
        logger.error(f"Error calling OpenRouter LLM: {e}", exc_info=True)
        return None
