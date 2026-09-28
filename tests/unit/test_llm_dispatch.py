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
from typing import Any, cast

import pytest

from services.agent.llm import dispatch as dispatch_module
from services.agent.llm.dispatch import (
    CHECK_AVAILABILITY_LOG_SUMMARY_KEYS,
    GET_QUOTE_LOG_SUMMARY_KEYS,
    NIGHT_RESULT_KEYS,
    QUOTE_RESULT_KEYS,
    _check_availability_log_summary,
    _get_quote_log_summary,
    _log_tool_call,
    dispatch_check_availability,
    dispatch_get_quote,
    dispatch_tool,
    quote_to_tool_result,
)
from services.agent.llm.errors import InvalidToolArgumentsError, UnknownToolError
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
        dispatch_check_availability(_NOT_A_CONNECTION, args)


def test_dispatch_check_availability_rejects_missing_field() -> None:
    args = {k: v for k, v in _VALID_ARGS.items() if k != "rooms"}
    with pytest.raises(InvalidToolArgumentsError, match="rooms"):
        dispatch_check_availability(_NOT_A_CONNECTION, args)


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


def test_quote_to_tool_result_contains_no_cost_bearing_field() -> None:
    """CLAUDE.md rule 2's enforcement point: even given a Quote with every
    cost field populated, the tool-facing dict is exactly the whitelist —
    nothing rides along by virtue of being an attribute on NightPrice."""
    result = quote_to_tool_result(_quote_with_full_cost_detail())

    assert result.keys() == QUOTE_RESULT_KEYS
    for night in result["nights"]:
        assert night.keys() == NIGHT_RESULT_KEYS

    serialized = repr(result)
    for forbidden in ("cost_per_night", "8000", "target_margin", "min_profit", "9600"):
        assert forbidden not in serialized


def test_quote_to_tool_result_formats_prices_as_display_strings_not_raw_integers() -> (
    None
):
    result = quote_to_tool_result(_quote_with_full_cost_detail())
    assert result["total_price_display"] == "150.00 SAR"
    assert result["nights"][0]["price_display"] == "150.00 SAR"
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
    proves the log summary narrows further still, to exactly quote_id and
    priced, not everything quote_to_tool_result happens to return."""
    result = quote_to_tool_result(_quote_with_full_cost_detail())
    summary = _get_quote_log_summary(result)

    assert summary.keys() == GET_QUOTE_LOG_SUMMARY_KEYS
    assert summary == {"priced": True, "quote_id": 42}


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
    }


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
        )

    records = [json.loads(r.getMessage()) for r in caplog.records]
    assert len(records) == 1
    record = records[0]
    assert record["tool_name"] == "made_up_tool"
    assert record["arguments"] == {"anything": "goes"}
    assert record["result_summary"] is None
    assert record["error_type"] == "UnknownToolError"


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
            _check_availability_log_summary,
            _get_quote_log_summary,
            dispatch_module.dispatch_tool,
        )
    )
    for forbidden in ("cost_per_night", "min_allowed", "target_margin", "min_profit"):
        assert forbidden not in logging_source
