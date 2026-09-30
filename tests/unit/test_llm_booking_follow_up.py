"""request_booking_follow_up's routing and validation in dispatch.py, and
the SQL contract with migration 0032. The database checks and the
one-per-quote guarantee run against Postgres in
tests/integration/test_booking_follow_up_integration.py."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from services.agent.llm import booking_follow_up as booking_module
from services.agent.llm import dispatch as dispatch_module
from services.agent.llm.booking_follow_up import (
    BOOKING_FOLLOW_UP_LOG_SUMMARY_KEYS,
    BOOKING_FOLLOW_UP_RESULT_KEYS,
    REASON_BOOKING_REQUESTED,
    REQUEST_BOOKING_FOLLOW_UP_TOOL,
)
from services.agent.llm.config import DEFAULT_QUOTE_VALIDITY_MINUTES
from services.agent.llm.dispatch import dispatch_tool
from services.agent.llm.errors import InvalidToolArgumentsError

_QUOTE_VALIDITY = timedelta(minutes=DEFAULT_QUOTE_VALIDITY_MINUTES)
_NOT_A_CONNECTION = cast(Any, object())
_MIGRATION_0032 = (
    Path(__file__).resolve().parents[2]
    / "db"
    / "migrations"
    / "0032_escalations_booking_request_quote.sql"
)


def _call(args: dict[str, Any], *, conversation_id: int | None) -> dict[str, Any]:
    return dispatch_tool(
        _NOT_A_CONNECTION,
        REQUEST_BOOKING_FOLLOW_UP_TOOL,
        args,
        now=cast(Any, None),
        customer_phone=None,
        conversation_id=conversation_id,
        resolved_stays=set(),
        quote_validity=_QUOTE_VALIDITY,
    )


def test_without_a_conversation_nothing_is_confirmable() -> None:
    with pytest.raises(InvalidToolArgumentsError) as exc_info:
        _call({}, conversation_id=None)

    assert exc_info.value.code == "quote_not_confirmable"


def _summary(quote_id: int) -> dict[str, Any]:
    return {
        "requested": True,
        "already_requested": False,
        "quote_id": quote_id,
        "hotel_name": "Test Hotel",
        "room_type_name": "Standard",
        "check_in": "2026-10-20",
        "check_out": "2026-10-22",
        "rooms": 1,
        "total_price_display": "200.00 SAR",
        "total_price_display_ar": "200.00 ريال",
    }


@pytest.mark.parametrize("args", [{}, {"quote_id": 999}, {"quote_id": "anything"}])
def test_the_model_cannot_name_a_quote(
    monkeypatch: pytest.MonkeyPatch, args: dict[str, Any]
) -> None:
    """The tool takes no arguments (owner decision 2026-09-30): whatever the
    model sends is ignored, and the database picks the quote."""
    seen: list[tuple[int, timedelta]] = []

    def _fake(
        _conn: Any, *, conversation_id: int, quote_validity: timedelta
    ) -> dict[str, Any]:
        seen.append((conversation_id, quote_validity))
        return _summary(7)

    monkeypatch.setattr(dispatch_module, "request_booking_follow_up", _fake)

    result = _call(args, conversation_id=3)

    assert seen == [(3, _QUOTE_VALIDITY)]
    assert result["quote_id"] == 7


def test_routes_to_the_booking_module_and_logs_only_the_summary(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        dispatch_module, "request_booking_follow_up", lambda *_a, **_k: _summary(7)
    )
    caplog.set_level(logging.INFO, logger=dispatch_module.logger.name)

    result = _call({}, conversation_id=3)

    assert result.keys() == BOOKING_FOLLOW_UP_RESULT_KEYS
    (event,) = [json.loads(r.getMessage()) for r in caplog.records]
    assert event["tool_name"] == REQUEST_BOOKING_FOLLOW_UP_TOOL
    assert event["result_summary"].keys() == BOOKING_FOLLOW_UP_LOG_SUMMARY_KEYS
    assert event["result_summary"] == {"quote_id": 7, "already_requested": False}


def test_the_insert_repeats_the_reason_and_the_index_predicate_literally() -> None:
    """Postgres uses the partial unique index as the conflict arbiter only
    when ON CONFLICT repeats its predicate; a drifted literal would make
    every insert fail instead of doing nothing."""
    sql = booking_module._INSERT_BOOKING_REQUEST_SQL
    literal = f"'{REASON_BOOKING_REQUESTED}'"
    assert sql.count(literal) == 2
    assert f"ON CONFLICT (quote_id) WHERE reason = {literal} DO NOTHING" in sql
    migration = _MIGRATION_0032.read_text(encoding="utf-8")
    assert f"WHERE reason = {literal};" in migration
