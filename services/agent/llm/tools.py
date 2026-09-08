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
"""

from __future__ import annotations

from google.genai import types

_DATE_DESCRIPTION = "An ISO 8601 date (YYYY-MM-DD)."

_STAY_PROPERTIES: dict[str, types.Schema] = {
    "hotel_id": types.Schema(type=types.Type.INTEGER, description="The hotel's id."),
    "room_type_id": types.Schema(
        type=types.Type.INTEGER, description="The room type's id."
    ),
    "check_in": types.Schema(
        type=types.Type.STRING, description=f"Check-in date. {_DATE_DESCRIPTION}"
    ),
    "check_out": types.Schema(
        type=types.Type.STRING,
        description=f"Check-out date (exclusive). {_DATE_DESCRIPTION}",
    ),
    "rooms": types.Schema(
        type=types.Type.INTEGER,
        description="Number of rooms requested.",
        minimum=1,
    ),
}

_STAY_REQUIRED = ["hotel_id", "room_type_id", "check_in", "check_out", "rooms"]

CHECK_AVAILABILITY = types.FunctionDeclaration(
    name="check_availability",
    description=(
        "Checks whether the requested number of rooms is available for "
        "every night of a stay. Returns only a yes/no answer — call "
        "get_quote separately for pricing."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties=dict(_STAY_PROPERTIES),
        required=list(_STAY_REQUIRED),
    ),
)

GET_QUOTE = types.FunctionDeclaration(
    name="get_quote",
    description=(
        "Prices a stay and returns the price to quote the customer. The "
        "returned price is already final and already formatted — never "
        "recompute, convert, or round it yourself."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties=dict(_STAY_PROPERTIES),
        required=list(_STAY_REQUIRED),
    ),
)

AGENT_TOOLS: tuple[types.Tool, ...] = (
    types.Tool(function_declarations=[CHECK_AVAILABILITY, GET_QUOTE]),
)
