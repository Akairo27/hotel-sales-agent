from __future__ import annotations

from typing import get_args

from services.agent.llm.booking_follow_up import REQUEST_BOOKING_FOLLOW_UP_TOOL
from services.agent.llm.dispatch import (
    CHECK_AVAILABILITY_TOOL,
    GET_QUOTE_TOOL,
    SEARCH_HOTELS_TOOL,
)
from services.agent.llm.errors import ToolErrorCode
from services.agent.llm.tools import (
    AGENT_TOOLS,
    CHECK_AVAILABILITY,
    GET_QUOTE,
    REQUEST_BOOKING_FOLLOW_UP,
    SEARCH_HOTELS,
    TOOL_ERROR_MESSAGES,
)

_EXPECTED_STAY_ARGS = {"hotel_id", "room_type_id", "check_in", "check_out", "rooms"}
_EXPECTED_SEARCH_HOTELS_ARGS = {
    "hotel_name",
    "city",
    "zone",
    "min_star_rating",
    "max_star_rating",
}


def test_check_availability_name_matches_dispatch_routing() -> None:
    assert CHECK_AVAILABILITY.name == CHECK_AVAILABILITY_TOOL


def test_get_quote_name_matches_dispatch_routing() -> None:
    assert GET_QUOTE.name == GET_QUOTE_TOOL


def test_search_hotels_name_matches_dispatch_routing() -> None:
    assert SEARCH_HOTELS.name == SEARCH_HOTELS_TOOL


def test_check_availability_required_args_match_what_dispatch_parses() -> None:
    assert set(CHECK_AVAILABILITY.parameters["required"]) == _EXPECTED_STAY_ARGS
    assert (
        set(CHECK_AVAILABILITY.parameters["properties"].keys()) == _EXPECTED_STAY_ARGS
    )


def test_get_quote_required_args_match_what_dispatch_parses() -> None:
    assert set(GET_QUOTE.parameters["required"]) == _EXPECTED_STAY_ARGS
    assert set(GET_QUOTE.parameters["properties"].keys()) == _EXPECTED_STAY_ARGS


def test_search_hotels_declares_every_filter_and_requires_none_individually() -> None:
    # "At least one filter" is a dispatch.py validation (InvalidToolArgumentsError),
    # the same way check_out > check_in is -- not expressible as plain JSON
    # Schema "required", so the schema itself requires nothing.
    assert set(SEARCH_HOTELS.parameters["properties"].keys()) == (
        _EXPECTED_SEARCH_HOTELS_ARGS
    )
    assert SEARCH_HOTELS.parameters["required"] == []


def test_search_hotels_city_and_zone_are_closed_lists() -> None:
    properties = SEARCH_HOTELS.parameters["properties"]
    assert set(properties["city"]["enum"]) == {"makkah", "madinah"}
    assert set(properties["zone"]["enum"]) == {
        "makkah_central",
        "makkah_outside",
        "madinah_central",
        "madinah_north",
        "madinah_west",
        "madinah_south",
        "madinah_outside",
    }


def test_get_quote_description_says_it_checks_availability_itself() -> None:
    """dispatch_get_quote declines to price a stay whose rooms are not free
    (services/agent/llm/dispatch.py) -- the model only knows that if the
    description tells it, and a description that quietly stops saying so
    would bring back the model offering to "check availability" after
    quoting a price."""
    description = GET_QUOTE.description
    assert "checks availability itself" in description
    assert "priced=false" in description


def test_tool_error_messages_cover_exactly_the_tool_error_codes() -> None:
    """One fixed message per ToolErrorCode (owner decisions: three
    messages, then quote_not_confirmable with request_booking_follow_up)
    -- a code added without a message would KeyError mid-turn, and a
    message without a code could never be sent."""
    assert set(TOOL_ERROR_MESSAGES) == set(get_args(ToolErrorCode))
    for message in TOOL_ERROR_MESSAGES.values():
        assert message.startswith("Not done:")


def test_agent_tools_declares_exactly_the_approved_tools() -> None:
    """PLAN.md's المرحلة ٤ scopes the agent to check_availability and
    get_quote; search_hotels is the prerequisite id-resolution tool added
    alongside them (2026-09-28), and request_booking_follow_up the
    owner-approved booking handoff (2026-09-30). search_alternatives has no
    implementation anywhere in the repo yet and must not be declared until
    it does."""
    names = {declaration.name for declaration in AGENT_TOOLS}
    assert names == {
        SEARCH_HOTELS_TOOL,
        CHECK_AVAILABILITY_TOOL,
        GET_QUOTE_TOOL,
        REQUEST_BOOKING_FOLLOW_UP_TOOL,
    }


def test_request_booking_follow_up_name_matches_dispatch_routing() -> None:
    assert REQUEST_BOOKING_FOLLOW_UP.name == REQUEST_BOOKING_FOLLOW_UP_TOOL


def test_request_booking_follow_up_takes_no_arguments() -> None:
    """The model never names a quote (owner decision 2026-09-30): the
    database picks the latest valid one the customer answered."""
    parameters = REQUEST_BOOKING_FOLLOW_UP.parameters
    assert parameters["properties"] == {}
    assert parameters["required"] == []


def test_request_booking_follow_up_description_demands_a_clear_yes() -> None:
    """Only after the customer clearly says yes, in any wording; the
    database check in booking_follow_up.py backs this up."""
    description = REQUEST_BOOKING_FOLLOW_UP.description
    assert "only when the customer has clearly said yes" in description
    assert "in any wording or dialect" in description
    assert "It takes no arguments" in description
    assert "restate exactly those" in description
    assert "books, holds and charges nothing" in description


def test_get_quote_description_names_every_reply_field() -> None:
    for field in (
        "hotel_name",
        "room_type_name",
        "night_count",
        "price_per_night_display",
        "lowest_night_price_display",
        "highest_night_price_display",
        "distance_to_haram_display",
    ):
        assert field in GET_QUOTE.description
