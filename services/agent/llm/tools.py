"""The versioned, reviewed tool-calling contract — CLAUDE.md §9: "Tool
definitions live in one file, versioned, and reviewed."

Three tools exist here: search_hotels, check_availability and get_quote.
PLAN.md's المرحلة ٤ (WhatsApp channel, read-only) scopes the agent to
exactly three tools — check_availability, get_quote, search_alternatives —
but search_hotels is a distinct, prerequisite capability those three names
never covered: resolving a hotel/room type the customer named into the
numeric ids check_availability and get_quote require. Added 2026-09-28
after an incident where the model, given no way to do that resolution,
guessed ids that did not exist. search_alternatives itself ("alternative
hotels for the same dates", per ARCHITECTURE.md's tool table) still has no
implementation anywhere in the repository, so it is still not declared
here. Adding a tool is a new entry in this file and a new case in
dispatch.py — never an inline capability added elsewhere, and never
without asking first (CLAUDE.md rule 10: "Adding a new tool the LLM can
call").

Declarations only: this module never executes anything. See dispatch.py.

parameters is plain JSON Schema (services.agent.llm.model_types.
ToolDeclaration), not a specific provider's own schema type —
services.agent.llm.client.py is the only place that translates this into
whichever provider's wire format a transport actually needs.
"""

from __future__ import annotations

from collections.abc import Mapping

from services.agent.llm.errors import ToolErrorCode
from services.agent.llm.model_types import ToolDeclaration

# Mirrors the closed lists migration 0028 enforces on hotels.city/hotels.zone
# (db/migrations/0028_hotel_location.sql) — kept as a plain tuple here rather
# than imported from anywhere, the same way this file never imports a SQL
# string: a tool schema is reviewed prose, not generated from the schema it
# describes.
_CITIES = ("makkah", "madinah")
_ZONES = (
    "makkah_central",
    "makkah_outside",
    "madinah_central",
    "madinah_north",
    "madinah_west",
    "madinah_south",
    "madinah_outside",
)

SEARCH_HOTELS = ToolDeclaration(
    name="search_hotels",
    description=(
        "Looks up real hotels and room types by name, city, zone and/or "
        "star rating. Always call this before check_availability or "
        "get_quote to resolve a hotel or room type the customer named — "
        "never invent or guess a hotel_id or room_type_id, including one "
        "the customer states directly as a number. If more than one hotel "
        "is returned, ask the customer which one they mean before calling "
        "any other tool. Returns no price and no cost."
    ),
    parameters={
        "type": "object",
        "properties": {
            "hotel_name": {
                "type": "string",
                "description": (
                    "All or part of the hotel's name, as the customer said it."
                ),
            },
            "city": {
                "type": "string",
                "enum": list(_CITIES),
                "description": "Restrict to hotels in this city.",
            },
            "zone": {
                "type": "string",
                "enum": list(_ZONES),
                "description": "Restrict to hotels in this zone.",
            },
            "min_star_rating": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "description": "Restrict to hotels rated at least this many stars.",
            },
            "max_star_rating": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "description": (
                    "Restrict to hotels rated at most this many stars. "
                    '"Economy" means 3 stars and below.'
                ),
            },
        },
        "required": [],
    },
)

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
        "every night of a stay. Returns a yes/no answer and, when the "
        "answer is no, the nights that stop the stay in two lists: "
        "unavailable_nights (open for booking, not enough free rooms) and "
        "nights_without_allotment (not open for booking yet). Never a "
        "price or a room count — call get_quote separately for pricing."
    ),
    parameters=_stay_parameters(),
)

GET_QUOTE = ToolDeclaration(
    name="get_quote",
    description=(
        "Prices a stay and returns the price to quote the customer. It "
        "checks availability itself: if the requested rooms are not free "
        "for every night, or the dates have no inventory, it returns "
        "priced=false with a reason, the nights that stop the stay "
        "(unavailable_nights, nights_without_allotment) and no price — "
        "never quote a price in that case. The returned price is already "
        "final and already formatted — never recompute, convert, or round "
        "it yourself."
    ),
    parameters=_stay_parameters(),
)

AGENT_TOOLS: tuple[ToolDeclaration, ...] = (
    SEARCH_HOTELS,
    CHECK_AVAILABILITY,
    GET_QUOTE,
)

# The only text the model ever receives when a tool call's arguments are
# rejected (services.agent.llm.dispatch.tool_error_result) -- one fixed,
# reviewed message per ToolErrorCode, owner-approved as three distinct
# messages (2026-09-29) so the model can tell a date problem from an id
# problem. Never the exception's own text: that can quote the model's (or
# a customer's) argument values back verbatim, which would hand injected
# text a second route into the model's context.
TOOL_ERROR_MESSAGES: Mapping[ToolErrorCode, str] = {
    "past_check_in": (
        "Not done: the check-in date is before today. Confirm the dates with "
        "the customer, then call the tool again with a check-in date of today "
        "or later."
    ),
    "unresolved_stay": (
        "Not done: this hotel_id and room_type_id were not returned by "
        "search_hotels in this conversation turn. Call search_hotels to find "
        "the real ids first; never guess an id or reuse one the customer "
        "typed."
    ),
    "invalid_arguments": (
        "Not done: the arguments for this tool call were invalid. Check every "
        "required field and its format, and ask the customer for anything you "
        "do not know instead of guessing."
    ),
}
