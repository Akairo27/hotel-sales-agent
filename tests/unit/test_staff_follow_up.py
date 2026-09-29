"""services/agent/staff_follow_up.py's nights_not_open_for_booking -- pure
over a turn's tool-call records. Opening the escalation itself is tested
end to end in tests/integration/test_webhook.py."""

from __future__ import annotations

from typing import Any

from services.agent.llm.conversation import ToolCallRecord
from services.agent.staff_follow_up import nights_not_open_for_booking


def _call(name: str, result: dict[str, Any]) -> ToolCallRecord:
    return ToolCallRecord(name=name, args={}, result=result)


def _not_open(hotel_id: int, room_type_id: int, nights: list[str]) -> dict[str, Any]:
    return {
        "hotel_id": hotel_id,
        "room_type_id": room_type_id,
        "unavailable_nights": [],
        "nights_without_allotment": nights,
    }


def test_nights_not_open_for_booking_is_empty_without_any_such_night() -> None:
    tool_calls = [
        _call("search_hotels", {"hotels": [], "truncated": False}),
        _call("check_availability", _not_open(1, 1, [])),
        _call("get_quote", {"priced": True, "quote_id": 7, "hotel_id": 1}),
        _call("get_quote", {"error": "invalid_arguments", "message": "..."}),
    ]
    assert nights_not_open_for_booking(tool_calls) == []


def test_nights_not_open_for_booking_merges_repeated_calls_for_one_stay() -> None:
    """check_availability and get_quote reporting the same nights (or
    overlapping ones) become one sorted, de-duplicated list."""
    tool_calls = [
        _call("check_availability", _not_open(1, 2, ["2030-02-02", "2030-02-01"])),
        _call("get_quote", _not_open(1, 2, ["2030-02-01", "2030-02-03"])),
    ]
    assert nights_not_open_for_booking(tool_calls) == [
        {
            "hotel_id": 1,
            "room_type_id": 2,
            "nights": ["2030-02-01", "2030-02-02", "2030-02-03"],
        }
    ]


def test_nights_not_open_for_booking_keeps_each_room_type_apart() -> None:
    tool_calls = [
        _call("get_quote", _not_open(3, 9, ["2030-02-01"])),
        _call("get_quote", _not_open(1, 2, ["2030-02-05"])),
    ]
    assert nights_not_open_for_booking(tool_calls) == [
        {"hotel_id": 1, "room_type_id": 2, "nights": ["2030-02-05"]},
        {"hotel_id": 3, "room_type_id": 9, "nights": ["2030-02-01"]},
    ]
