"""The versioned, reviewed tool-calling contract — CLAUDE.md §9: "Tool
definitions live in one file, versioned, and reviewed."

Only two tools exist here: check_availability and get_quote. PLAN.md's
المرحلة ٤ (WhatsApp channel, read-only) scopes the agent to exactly three
tools — check_availability, get_quote, search_alternatives — and
search_alternatives has no underlying implementation anywhere in the
repository yet, so it is not declared here. Adding it, or any other tool,
is a new entry in this file and a new case in dispatch.py — never an
inline capability added elsewhere, and never without asking first
(CLAUDE.md rule 10: "Adding a new tool the LLM can call").

Declarations only: this module never executes anything. See dispatch.py.

parameters is plain JSON Schema (services.agent.llm.model_types.
ToolDeclaration), not a specific provider's own schema type —
services.agent.llm.client.py is the only place that translates this into
whichever provider's wire format a transport actually needs.
"""

from __future__ import annotations

from services.agent.llm.model_types import ToolDeclaration

_DATE_DESCRIPTION = "An ISO 8601 date (YYYY-MM-DD)."

_STAY_PROPERTIES: dict[str, object] = {
    "hotel_id": {"type": "integer", "description": "The hotel's id."},
    "room_type_id": {"type": "integer", "description": "The room type's id."},
    "check_in": {
        "type": "string",
        "description": f"Check-in date. {_DATE_DESCRIPTION}",
    },
    "check_out": {
        "type": "string",
        "description": f"Check-out date (exclusive). {_DATE_DESCRIPTION}",
    },
    "rooms": {
        "type": "integer",
        "description": "Number of rooms requested.",
        "minimum": 1,
    },
}

_STAY_REQUIRED = ["hotel_id", "room_type_id", "check_in", "check_out", "rooms"]


def _stay_parameters() -> dict[str, object]:
    """A fresh dict every call — check_availability's and get_quote's
    parameters must never alias the same nested dict/list objects, so
    mutating one (accidentally, downstream) can never affect the other."""
    return {
        "type": "object",
        "properties": dict(_STAY_PROPERTIES),
        "required": list(_STAY_REQUIRED),
    }


CHECK_AVAILABILITY = ToolDeclaration(
    name="check_availability",
    description=(
        "Checks whether the requested number of rooms is available for "
        "every night of a stay. Returns only a yes/no answer — call "
        "get_quote separately for pricing."
    ),
    parameters=_stay_parameters(),
)

GET_QUOTE = ToolDeclaration(
    name="get_quote",
    description=(
        "Prices a stay and returns the price to quote the customer. The "
        "returned price is already final and already formatted — never "
        "recompute, convert, or round it yourself."
    ),
    parameters=_stay_parameters(),
)

AGENT_TOOLS: tuple[ToolDeclaration, ...] = (CHECK_AVAILABILITY, GET_QUOTE)
