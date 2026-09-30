"""Unit tests for dispatch.py that need no database at all: argument
validation happens before any service call, and quote_to_tool_result is a
pure function over an in-memory Quote. The `object()` sentinel used as
`conn` below stands in for "this path must never touch the database" —
any attempt to actually use it as a connection blows up loudly, which is
exactly what a leaked cost field or an unvalidated tool call reaching the
services would look like failing.
"""

from __future__ import annotations

import inspect
import json
import logging
from datetime import UTC, date, datetime
from typing import Any, cast, get_args

import pytest

from services.agent.llm import dispatch as dispatch_module
from services.agent.llm.dispatch import (
    CHECK_AVAILABILITY_LOG_SUMMARY_KEYS,
    GET_QUOTE_LOG_SUMMARY_KEYS,
    NIGHT_RESULT_KEYS,
    QUOTE_RESULT_KEYS,
    SEARCH_HOTELS_LOG_SUMMARY_KEYS,
    TOOL_ERROR_RESULT_KEYS,
    UNPRICED_RESULT_KEYS,
    StayArgs,
    UnpricedReason,
    _check_availability_log_summary,
    _get_quote_log_summary,
    _log_tool_call,
    _require_resolved_stay,
    _search_hotels_log_summary,
    dispatch_check_availability,
    dispatch_get_quote,
    dispatch_tool,
    parse_search_hotels_args,
    quote_to_tool_result,
    tool_error_result,
)
from services.agent.llm.errors import (
    InvalidToolArgumentsError,
    StayListingNotFoundError,
    ToolErrorCode,
    UnknownToolError,
)
from services.agent.llm.quote_display import QuoteListing
from services.agent.llm.tools import TOOL_ERROR_MESSAGES
from services.inventory.operations import StayAvailability
from services.pricing.compute import NightPrice, Quote

_NOT_A_CONNECTION = cast(Any, object())
_UNUSED_NOW = datetime(2026, 1, 1, tzinfo=UTC)  # validation raises before this is read

_VALID_ARGS: dict[str, Any] = {
    "hotel_id": 1,
    "room_type_id": 2,
    "check_in": "2026-09-01",
    "check_out": "2026-09-03",
    "rooms": 1,
}


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ({"hotel_id": "one"}, "hotel_id"),
        ({"rooms": 0}, "rooms must be positive"),
        ({"rooms": -1}, "rooms must be positive"),
        ({"check_in": "not-a-date"}, "check_in"),
        ({"check_out": "2026-08-31"}, "check_out must be after check_in"),
        ({"check_out": "2026-09-01"}, "check_out must be after check_in"),
    ],
)
def test_dispatch_check_availability_rejects_bad_args_without_touching_conn(
    mutation: dict[str, Any], match: str
) -> None:
    args = {**_VALID_ARGS, **mutation}
    with pytest.raises(InvalidToolArgumentsError, match=match):
        dispatch_check_availability(_NOT_A_CONNECTION, args, now=_UNUSED_NOW)


def test_dispatch_check_availability_rejects_missing_field() -> None:
    args = {k: v for k, v in _VALID_ARGS.items() if k != "rooms"}
    with pytest.raises(InvalidToolArgumentsError, match="rooms"):
        dispatch_check_availability(_NOT_A_CONNECTION, args, now=_UNUSED_NOW)


def test_dispatch_check_availability_rejects_a_past_check_in_before_any_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same backstop as get_quote (owner decision, 2026-09-30): a past
    date without a year must be confirmed with the customer, never answered
    with the availability of a night that has gone."""
    monkeypatch.setattr(dispatch_module, "stay_availability", _must_not_be_called)

    with pytest.raises(InvalidToolArgumentsError) as exc_info:
        dispatch_check_availability(
            _NOT_A_CONNECTION,
            _VALID_ARGS,  # check_in 2026-09-01
            now=datetime(2026, 10, 1, tzinfo=UTC),
        )

    assert exc_info.value.code == "past_check_in"


def test_unpriced_result_keys_match_the_whitelist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_stay_availability(monkeypatch, unavailable_nights=(date(2026, 9, 2),))

    result = dispatch_get_quote(
        _NOT_A_CONNECTION,
        _VALID_ARGS,
        now=_UNUSED_NOW,
        customer_phone=None,
        conversation_id=None,
    )

    assert result.keys() == UNPRICED_RESULT_KEYS


def test_dispatch_get_quote_rejects_bad_args_without_touching_conn() -> None:
    args = {**_VALID_ARGS, "rooms": 0}
    with pytest.raises(InvalidToolArgumentsError, match="rooms must be positive"):
        dispatch_get_quote(
            _NOT_A_CONNECTION,
            args,
            now=_UNUSED_NOW,
            customer_phone=None,
            conversation_id=None,
        )


def test_dispatch_tool_raises_on_unknown_tool_name_without_touching_conn() -> None:
    with pytest.raises(UnknownToolError, match="made_up_tool"):
        dispatch_tool(
            _NOT_A_CONNECTION,
            "made_up_tool",
            {},
            now=_UNUSED_NOW,
            customer_phone=None,
            conversation_id=None,
            resolved_stays=set(),
        )


def test_dispatch_tool_rejects_an_unresolved_check_availability_stay() -> None:
    """The regression test for the incident this whole feature exists to
    prevent: a hotel_id/room_type_id pair search_hotels never returned
    this turn must be rejected before ever reaching the database, model
    guess or customer-stated number alike."""
    with pytest.raises(InvalidToolArgumentsError, match="was not returned"):
        dispatch_tool(
            _NOT_A_CONNECTION,
            "check_availability",
            _VALID_ARGS,
            now=_UNUSED_NOW,
            customer_phone=None,
            conversation_id=None,
            resolved_stays=set(),
        )


def test_dispatch_tool_rejects_an_unresolved_get_quote_stay() -> None:
    with pytest.raises(InvalidToolArgumentsError, match="was not returned"):
        dispatch_tool(
            _NOT_A_CONNECTION,
            "get_quote",
            _VALID_ARGS,
            now=_UNUSED_NOW,
            customer_phone=None,
            conversation_id=None,
            resolved_stays=set(),
        )


def _quote_with_full_cost_detail() -> Quote:
    """A Quote whose nights carry every cost-bearing field NightPrice can
    have populated — the shape a real, non-overridden compute_quote
    result has. quote_to_tool_result must still emit only the whitelisted
    keys below."""
    night = NightPrice(
        stay_date=date(2026, 9, 1),
        season_id=1,
        ask=15_000,
        min_allowed=11_000,
        override_applied=False,
        cost_per_night=8_000,
        occupancy=0.4,
        target_margin_bps=2_000,
        target_margin_rule_id=7,
        price_after_margin=9_600,
        occupancy_multiplier_bps=10_000,
        lead_time_multiplier_bps=15_625,
        demand_factor_bps=15_625,
        demand_curve_rule_id=3,
        min_profit_halalas=3_000,
        min_profit_rule_id=9,
    )
    return Quote(
        id=42,
        hotel_id=1,
        room_type_id=2,
        check_in=date(2026, 9, 1),
        check_out=date(2026, 9, 2),
        rooms=1,
        ask_price_total=15_000,
        min_allowed_total=11_000,
        nights=[night],
        negotiation_open=True,
    )


_LISTING = QuoteListing(
    hotel_name="Test Hotel",
    room_type_name="Standard",
    city="makkah",
    distance_to_haram_meters=350,
)


def test_quote_to_tool_result_contains_no_cost_bearing_field() -> None:
    """CLAUDE.md rule 2's enforcement point: even given a Quote with every
    cost field populated, the tool-facing dict is exactly the whitelist —
    nothing rides along by virtue of being an attribute on NightPrice."""
    result = quote_to_tool_result(_quote_with_full_cost_detail(), _LISTING)

    assert result.keys() == QUOTE_RESULT_KEYS
    for night in result["nights"]:
        assert night.keys() == NIGHT_RESULT_KEYS

    serialized = repr(result)
    for forbidden in ("cost_per_night", "8000", "target_margin", "min_profit", "9600"):
        assert forbidden not in serialized


def test_quote_to_tool_result_formats_prices_as_display_strings_not_raw_integers() -> (
    None
):
    result = quote_to_tool_result(_quote_with_full_cost_detail(), _LISTING)
    assert result["total_price_display"] == "150.00 SAR"
    assert result["nights"][0]["price_display"] == "150.00 SAR"
    assert result["total_price_display_ar"] == "150.00 ريال"
    assert result["nights"][0]["price_display_ar"] == "150.00 ريال"
    assert isinstance(result["total_price_display"], str)


def test_check_availability_log_summary_contains_only_the_whitelisted_key() -> None:
    result = {
        "available": True,
        "hotel_id": 1,
        "room_type_id": 2,
        "check_in": "2026-09-01",
        "check_out": "2026-09-03",
        "rooms": 1,
    }
    assert (
        _check_availability_log_summary(result).keys()
        == CHECK_AVAILABILITY_LOG_SUMMARY_KEYS
    )
    assert _check_availability_log_summary(result) == {"available": True}


def test_get_quote_log_summary_contains_no_cost_bearing_field() -> None:
    """Fed the full, already-cost-free tool result (itself proven cost-free
    by test_quote_to_tool_result_contains_no_cost_bearing_field above) --
    proves the log summary narrows further still, to exactly priced,
    quote_id and reason (None for a priced result), not everything
    quote_to_tool_result happens to return."""
    result = quote_to_tool_result(_quote_with_full_cost_detail(), _LISTING)
    summary = _get_quote_log_summary(result)

    assert summary.keys() == GET_QUOTE_LOG_SUMMARY_KEYS
    assert summary == {"priced": True, "quote_id": 42, "reason": None}


def test_get_quote_log_summary_reports_quote_id_none_when_unpriced() -> None:
    unpriced_result = {
        "priced": False,
        "reason": "no_allotment_for_dates",
        "hotel_id": 1,
        "room_type_id": 2,
        "check_in": "2026-09-01",
        "check_out": "2026-09-03",
    }
    assert _get_quote_log_summary(unpriced_result) == {
        "priced": False,
        "quote_id": None,
        "reason": "no_allotment_for_dates",
    }


@pytest.mark.parametrize("reason", get_args(UnpricedReason))
def test_get_quote_log_summary_carries_every_unpriced_reason_unchanged(
    reason: str,
) -> None:
    """The reason is logged as the fixed literal _unpriced_result put in the
    result -- one case per member of UnpricedReason, so a reason added to
    that closed set later is exercised here without editing this test."""
    unpriced_result = {
        "priced": False,
        "reason": reason,
        "hotel_id": 1,
        "room_type_id": 2,
        "check_in": "2026-09-01",
        "check_out": "2026-09-03",
    }
    summary = _get_quote_log_summary(unpriced_result)

    assert summary.keys() == GET_QUOTE_LOG_SUMMARY_KEYS
    assert summary == {"priced": False, "quote_id": None, "reason": reason}


def _must_not_be_called(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("this call must not happen on this path")


def _stub_stay_availability(
    monkeypatch: pytest.MonkeyPatch,
    *,
    unavailable_nights: tuple[date, ...] = (),
    nights_without_allotment: tuple[date, ...] = (),
) -> list[tuple[Any, ...]]:
    """Makes dispatch's one inventory read return the given nights, and
    records every call's arguments."""
    calls: list[tuple[Any, ...]] = []

    def _stay_availability(*args: Any) -> StayAvailability:
        calls.append(args)
        return StayAvailability(
            unavailable_nights=unavailable_nights,
            nights_without_allotment=nights_without_allotment,
        )

    monkeypatch.setattr(dispatch_module, "stay_availability", _stay_availability)
    return calls


def test_dispatch_get_quote_declines_without_pricing_when_rooms_are_not_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate sits before compute_quote: compute_quote is what writes the
    `quotes` row, so never reaching it is what guarantees no row is written
    for a stay the inventory cannot cover. The result names the short night
    by date and carries no room count -- exactly the ids, dates and the two
    night lists, nothing about how many rooms are free."""
    calls = _stub_stay_availability(monkeypatch, unavailable_nights=(date(2026, 9, 2),))
    monkeypatch.setattr(dispatch_module, "compute_quote", _must_not_be_called)

    result = dispatch_get_quote(
        _NOT_A_CONNECTION,
        _VALID_ARGS,
        now=_UNUSED_NOW,
        customer_phone=None,
        conversation_id=None,
    )

    assert result == {
        "priced": False,
        "reason": "insufficient_availability",
        "hotel_id": 1,
        "room_type_id": 2,
        "check_in": "2026-09-01",
        "check_out": "2026-09-03",
        "unavailable_nights": ["2026-09-02"],
        "nights_without_allotment": [],
    }
    assert calls == [(_NOT_A_CONNECTION, 1, 2, date(2026, 9, 1), date(2026, 9, 3), 1)]


def test_dispatch_get_quote_reports_missing_inventory_ahead_of_short_nights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stay with a night not open for booking is reported as
    no_allotment_for_dates even when another night is merely short -- that
    night is not "fully booked", and the customer must hear the difference.
    Both lists are still returned."""
    _stub_stay_availability(
        monkeypatch,
        unavailable_nights=(date(2026, 9, 1),),
        nights_without_allotment=(date(2026, 9, 2),),
    )
    monkeypatch.setattr(dispatch_module, "compute_quote", _must_not_be_called)

    result = dispatch_get_quote(
        _NOT_A_CONNECTION,
        _VALID_ARGS,
        now=_UNUSED_NOW,
        customer_phone=None,
        conversation_id=None,
    )

    assert result["priced"] is False
    assert result["reason"] == "no_allotment_for_dates"
    assert result["unavailable_nights"] == ["2026-09-01"]
    assert result["nights_without_allotment"] == ["2026-09-02"]


def test_dispatch_get_quote_prices_when_rooms_are_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_stay_availability(monkeypatch)
    monkeypatch.setattr(
        dispatch_module, "load_quote_listing", lambda *_args, **_kwargs: _LISTING
    )
    monkeypatch.setattr(
        dispatch_module,
        "compute_quote",
        lambda *_args, **_kwargs: _quote_with_full_cost_detail(),
    )

    result = dispatch_get_quote(
        _NOT_A_CONNECTION,
        _VALID_ARGS,
        now=_UNUSED_NOW,
        customer_phone=None,
        conversation_id=None,
    )

    assert result["priced"] is True
    assert result["quote_id"] == 42
    assert result["hotel_name"] == "Test Hotel"
    assert result["room_type_name"] == "Standard"
    assert result["night_count"] == 1
    assert result["price_per_night_display"] == "150.00 SAR"
    assert result["distance_to_haram_display"] == "350 m"


def test_dispatch_get_quote_rejects_a_past_check_in_before_any_inventory_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Date validation runs ahead of every inventory decision: neither the
    inventory read nor compute_quote may be reached for a check_in before
    today (each is stubbed to fail loudly if it is)."""
    monkeypatch.setattr(dispatch_module, "stay_availability", _must_not_be_called)
    monkeypatch.setattr(dispatch_module, "compute_quote", _must_not_be_called)

    with pytest.raises(InvalidToolArgumentsError) as exc_info:
        dispatch_get_quote(
            _NOT_A_CONNECTION,
            _VALID_ARGS,  # check_in 2026-09-01
            now=datetime(2026, 10, 1, tzinfo=UTC),
            customer_phone=None,
            conversation_id=None,
        )

    assert str(exc_info.value) == "check_in must not be in the past"
    assert exc_info.value.code == "past_check_in"


def test_dispatch_get_quote_reads_the_listing_before_writing_any_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hotel or room type gone since search_hotels fails the call before
    compute_quote runs, so no quote row is written for a stay the reply
    could not name."""
    _stub_stay_availability(monkeypatch)

    def _gone(*_args: Any, **_kwargs: Any) -> Any:
        raise StayListingNotFoundError("gone")

    monkeypatch.setattr(dispatch_module, "load_quote_listing", _gone)
    monkeypatch.setattr(dispatch_module, "compute_quote", _must_not_be_called)

    with pytest.raises(StayListingNotFoundError):
        dispatch_get_quote(
            _NOT_A_CONNECTION,
            _VALID_ARGS,
            now=_UNUSED_NOW,
            customer_phone=None,
            conversation_id=None,
        )


def test_dispatch_get_quote_accepts_a_check_in_of_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary of the date rule: check_in equal to now's date is not
    in the past (strictly earlier is), so the stay goes on to be priced."""
    _stub_stay_availability(monkeypatch)
    monkeypatch.setattr(
        dispatch_module, "load_quote_listing", lambda *_args, **_kwargs: _LISTING
    )
    monkeypatch.setattr(
        dispatch_module,
        "compute_quote",
        lambda *_args, **_kwargs: _quote_with_full_cost_detail(),
    )

    result = dispatch_get_quote(
        _NOT_A_CONNECTION,
        _VALID_ARGS,  # check_in 2026-09-01
        now=datetime(2026, 9, 1, 23, 59, tzinfo=UTC),
        customer_phone=None,
        conversation_id=None,
    )

    assert result["priced"] is True


def test_dispatch_tool_logs_and_reraises_invalid_tool_arguments_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.llm.dispatch")
    bad_args = {**_VALID_ARGS, "rooms": 0}

    with pytest.raises(InvalidToolArgumentsError):
        dispatch_tool(
            _NOT_A_CONNECTION,
            "check_availability",
            bad_args,
            now=_UNUSED_NOW,
            customer_phone=None,
            conversation_id=7,
            resolved_stays=set(),
        )

    records = [json.loads(r.getMessage()) for r in caplog.records]
    assert len(records) == 1
    record = records[0]
    assert record["event"] == "agent_tool_call"
    assert record["conversation_id"] == 7
    assert record["tool_name"] == "check_availability"
    assert record["arguments"] == bad_args
    assert record["result_summary"] is None
    assert record["error_type"] == "InvalidToolArgumentsError"
    assert record["error_code"] == "invalid_arguments"


def test_dispatch_tool_logs_and_reraises_unknown_tool_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.llm.dispatch")

    with pytest.raises(UnknownToolError):
        dispatch_tool(
            _NOT_A_CONNECTION,
            "made_up_tool",
            {"anything": "goes"},
            now=_UNUSED_NOW,
            customer_phone=None,
            conversation_id=None,
            resolved_stays=set(),
        )

    records = [json.loads(r.getMessage()) for r in caplog.records]
    assert len(records) == 1
    record = records[0]
    assert record["tool_name"] == "made_up_tool"
    assert record["arguments"] == {"anything": "goes"}
    assert record["result_summary"] is None
    assert record["error_type"] == "UnknownToolError"
    assert record["error_code"] is None


def test_dispatch_tool_logs_unresolved_stay_code_for_an_id_search_hotels_never_returned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The resolved-stays guard raises before the connection is touched,
    tagged unresolved_stay -- the code that picks the fixed message telling
    the model to call search_hotels first."""
    caplog.set_level(logging.INFO, logger="services.agent.llm.dispatch")

    with pytest.raises(InvalidToolArgumentsError) as exc_info:
        dispatch_tool(
            _NOT_A_CONNECTION,
            "check_availability",
            _VALID_ARGS,
            now=_UNUSED_NOW,
            customer_phone=None,
            conversation_id=None,
            resolved_stays=set(),
        )

    assert exc_info.value.code == "unresolved_stay"
    (record,) = [json.loads(r.getMessage()) for r in caplog.records]
    assert record["error_code"] == "unresolved_stay"


@pytest.mark.parametrize("code", get_args(ToolErrorCode))
def test_tool_error_result_is_the_fixed_message_for_its_code_and_nothing_else(
    code: ToolErrorCode,
) -> None:
    result = tool_error_result(code)

    assert result.keys() == TOOL_ERROR_RESULT_KEYS
    assert result == {"error": code, "message": TOOL_ERROR_MESSAGES[code]}


def test_invalid_tool_arguments_error_defaults_to_the_generic_code() -> None:
    assert InvalidToolArgumentsError("anything").code == "invalid_arguments"


def test_logging_code_never_references_a_floor_or_cost_field_by_name() -> None:
    """Belt-and-suspenders alongside the whitelist tests above: scans the
    actual source of the functions that build and emit the logged record
    (not the whole module -- dispatch.py's own docstring legitimately
    names these fields in prose) for the literal field names CLAUDE.md
    rule 2 says must never reach a log the model can read."""
    logging_source = "".join(
        inspect.getsource(func)
        for func in (
            _log_tool_call,
            _search_hotels_log_summary,
            _check_availability_log_summary,
            _get_quote_log_summary,
            dispatch_module.dispatch_tool,
        )
    )
    for forbidden in ("cost_per_night", "min_allowed", "target_margin", "min_profit"):
        assert forbidden not in logging_source


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ({"city": "narnia"}, "city"),
        ({"zone": "makkah_north"}, "zone"),
        ({"min_star_rating": 0}, "min_star_rating"),
        ({"min_star_rating": 6}, "min_star_rating"),
        ({"min_star_rating": "four"}, "min_star_rating"),
        ({"max_star_rating": 0}, "max_star_rating"),
        ({"hotel_name": 5}, "hotel_name"),
    ],
)
def test_parse_search_hotels_args_rejects_bad_values(
    mutation: dict[str, Any], match: str
) -> None:
    with pytest.raises(InvalidToolArgumentsError, match=match):
        parse_search_hotels_args({"hotel_name": "test", **mutation})


def test_parse_search_hotels_args_requires_at_least_one_filter() -> None:
    with pytest.raises(InvalidToolArgumentsError, match="at least one filter"):
        parse_search_hotels_args({})


def test_parse_search_hotels_args_rejects_min_above_max_star_rating() -> None:
    with pytest.raises(InvalidToolArgumentsError, match="min_star_rating"):
        parse_search_hotels_args({"min_star_rating": 4, "max_star_rating": 3})


def test_parse_search_hotels_args_accepts_a_single_filter() -> None:
    search = parse_search_hotels_args({"city": "makkah"})
    assert search.city == "makkah"
    assert search.hotel_name is None
    assert search.min_star_rating is None
    assert search.max_star_rating is None


def test_search_hotels_log_summary_contains_only_the_whitelisted_keys() -> None:
    result = {
        "hotels": [
            {"hotel_id": 1, "room_types": []},
            {"hotel_id": 2, "room_types": []},
        ],
        "truncated": False,
    }
    summary = _search_hotels_log_summary(result)
    assert summary.keys() == SEARCH_HOTELS_LOG_SUMMARY_KEYS
    assert summary == {"result_count": 2, "truncated": False}


def test_require_resolved_stay_accepts_a_resolved_pair() -> None:
    stay = StayArgs(
        hotel_id=1,
        room_type_id=2,
        check_in=date(2026, 9, 1),
        check_out=date(2026, 9, 3),
        rooms=1,
    )
    _require_resolved_stay(stay, {(1, 2)})  # must not raise


def test_require_resolved_stay_rejects_an_unresolved_pair() -> None:
    stay = StayArgs(
        hotel_id=1,
        room_type_id=2,
        check_in=date(2026, 9, 1),
        check_out=date(2026, 9, 3),
        rooms=1,
    )
    with pytest.raises(
        InvalidToolArgumentsError, match="was not returned by search_hotels"
    ):
        _require_resolved_stay(stay, {(1, 3)})  # right hotel, wrong room type
    with pytest.raises(
        InvalidToolArgumentsError, match="was not returned by search_hotels"
    ):
        _require_resolved_stay(stay, set())
