"""Executes a model tool call against the real inventory and pricing code
— CLAUDE.md rules 1 and 2.

This is where those two rules are actually enforced, not just documented:
- Rule 1 (the model never computes a price): every price handed back is
  already final and already formatted by lib/money.py. The model receives
  a string like "1,250.00 SAR", never a bare integer it could do
  arithmetic on.
- Rule 2 (cost never enters the LLM context): compute_quote returns Quote/
  NightPrice objects that carry cost_per_night, target_margin_bps,
  min_profit_halalas, and the rest of the audit trail recorded in
  `quotes` (services/pricing/compute.py). quote_to_tool_result below never
  reads any of those attributes — it builds the outgoing dict key by key,
  by hand, so a field added to NightPrice later cannot ride along into
  the model's context just by existing on the dataclass.

Arguments come from the model, which can hallucinate types, omit
required fields, or send a date range that fails the underlying
services' own validation — all of that is InvalidToolArgumentsError, an
expected failure mode of a function-calling model, not a bug here. An
AllotmentNotFoundError from get_quote (no allotment configured for those
dates at all) is likewise reported back as an unpriced result rather than
raised, so the model can tell the customer rather than the whole turn
failing. Every other pricing exception (a price_rules misconfiguration —
IncompletePriceRuleChainError, NoMatchingBandError,
InconsistentPriceConfigurationError) is a business-data problem, not a
customer-facing outcome, and is deliberately left to propagate: this
module has no escalate tool to route it to — this PR scopes the agent to
check_availability and get_quote only (see tools.py's module docstring
for why search_alternatives, the third tool PLAN.md's المرحلة ٤ names,
is not declared yet), so the caller crashing the turn is more honest than
inventing a way to paper over it here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import psycopg

from lib.money import format_halalas_as_sar
from services.agent.llm.errors import InvalidToolArgumentsError, UnknownToolError
from services.inventory.operations import check_availability
from services.pricing.compute import Quote, compute_quote
from services.pricing.errors import AllotmentNotFoundError

CHECK_AVAILABILITY_TOOL = "check_availability"
GET_QUOTE_TOOL = "get_quote"

# The exact key set quote_to_tool_result may ever produce — the
# enforcement point tests/unit/test_llm_dispatch.py checks rule 2
# against.
QUOTE_RESULT_KEYS = frozenset(
    {
        "priced",
        "quote_id",
        "hotel_id",
        "room_type_id",
        "check_in",
        "check_out",
        "rooms",
        "total_price_display",
        "nights",
        "negotiation_open",
    }
)
UNPRICED_RESULT_KEYS = frozenset(
    {"priced", "reason", "hotel_id", "room_type_id", "check_in", "check_out"}
)
NIGHT_RESULT_KEYS = frozenset({"date", "price_display"})


@dataclass(frozen=True)
class StayArgs:
    """Parsed, validated arguments shared by both tools' schemas."""

    hotel_id: int
    room_type_id: int
    check_in: date
    check_out: date
    rooms: int


def _require_int(args: dict[str, Any], key: str) -> int:
    value = args.get(key)
    if isinstance(value, bool):
        raise InvalidToolArgumentsError(f"{key} must be an integer, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise InvalidToolArgumentsError(f"{key} must be an integer, got {value!r}")


def _require_date(args: dict[str, Any], key: str) -> date:
    value = args.get(key)
    if not isinstance(value, str):
        raise InvalidToolArgumentsError(f"{key} must be a string, got {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidToolArgumentsError(
            f"{key}={value!r} is not a valid ISO 8601 date"
        ) from exc


def parse_stay_args(args: dict[str, Any]) -> StayArgs:
    """Validates the shared (hotel_id, room_type_id, check_in, check_out,
    rooms) shape both tool schemas declare.

    Raises:
        InvalidToolArgumentsError: a field is missing, the wrong type, or
            the parsed values fail a basic sanity check (check_out not
            after check_in, rooms not positive).
    """
    stay = StayArgs(
        hotel_id=_require_int(args, "hotel_id"),
        room_type_id=_require_int(args, "room_type_id"),
        check_in=_require_date(args, "check_in"),
        check_out=_require_date(args, "check_out"),
        rooms=_require_int(args, "rooms"),
    )
    if stay.check_out <= stay.check_in:
        raise InvalidToolArgumentsError("check_out must be after check_in")
    if stay.rooms <= 0:
        raise InvalidToolArgumentsError("rooms must be positive")
    return stay


def quote_to_tool_result(quote: Quote) -> dict[str, Any]:
    """Converts a priced Quote into the exact, cost-free dict shape sent
    to the model. See the module docstring for why this exists.
    """
    return {
        "priced": True,
        "quote_id": quote.id,
        "hotel_id": quote.hotel_id,
        "room_type_id": quote.room_type_id,
        "check_in": quote.check_in.isoformat(),
        "check_out": quote.check_out.isoformat(),
        "rooms": quote.rooms,
        "total_price_display": format_halalas_as_sar(quote.ask_price_total),
        "nights": [
            {
                "date": night.stay_date.isoformat(),
                "price_display": format_halalas_as_sar(night.ask),
            }
            for night in quote.nights
        ],
        "negotiation_open": quote.negotiation_open,
    }


def _unpriced_result(stay: StayArgs, *, reason: str) -> dict[str, Any]:
    return {
        "priced": False,
        "reason": reason,
        "hotel_id": stay.hotel_id,
        "room_type_id": stay.room_type_id,
        "check_in": stay.check_in.isoformat(),
        "check_out": stay.check_out.isoformat(),
    }


def dispatch_check_availability(
    conn: psycopg.Connection[Any], args: dict[str, Any]
) -> dict[str, Any]:
    """Executes check_availability. Never touches cost — this tool never
    returns anything price-related at all."""
    stay = parse_stay_args(args)
    available = check_availability(
        conn,
        stay.hotel_id,
        stay.room_type_id,
        stay.check_in,
        stay.check_out,
        stay.rooms,
    )
    return {
        "available": available,
        "hotel_id": stay.hotel_id,
        "room_type_id": stay.room_type_id,
        "check_in": stay.check_in.isoformat(),
        "check_out": stay.check_out.isoformat(),
        "rooms": stay.rooms,
    }


def dispatch_get_quote(
    conn: psycopg.Connection[Any],
    args: dict[str, Any],
    *,
    now: datetime,
    customer_phone: str | None,
    conversation_id: int | None,
) -> dict[str, Any]:
    """Executes get_quote: prices the stay, records it in `quotes`
    (compute_quote's own responsibility), and returns the cost-free
    result the model may relay to the customer.
    """
    stay = parse_stay_args(args)
    try:
        quote = compute_quote(
            conn,
            stay.hotel_id,
            stay.room_type_id,
            stay.check_in,
            stay.check_out,
            stay.rooms,
            now,
            customer_phone=customer_phone,
            conversation_id=conversation_id,
        )
    except ValueError as exc:
        raise InvalidToolArgumentsError(str(exc)) from exc
    except AllotmentNotFoundError:
        return _unpriced_result(stay, reason="no_allotment_for_dates")

    return quote_to_tool_result(quote)


def dispatch_tool(
    conn: psycopg.Connection[Any],
    name: str,
    args: dict[str, Any],
    *,
    now: datetime,
    customer_phone: str | None,
    conversation_id: int | None,
) -> dict[str, Any]:
    """Routes a model tool call by name to its handler.

    Raises:
        UnknownToolError: name is not one of the tools declared in
            tools.py. Never executed silently.
        InvalidToolArgumentsError: see the individual dispatch functions.
    """
    if name == CHECK_AVAILABILITY_TOOL:
        return dispatch_check_availability(conn, args)
    if name == GET_QUOTE_TOOL:
        return dispatch_get_quote(
            conn,
            args,
            now=now,
            customer_phone=customer_phone,
            conversation_id=conversation_id,
        )
    raise UnknownToolError(f"model called unknown tool {name!r}")
