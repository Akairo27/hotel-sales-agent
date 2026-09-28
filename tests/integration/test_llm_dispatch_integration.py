"""Integration tests for dispatch.py against real pricing/inventory data —
the end-to-end path from a model tool call to a real `quotes` row, with
the returned tool-facing payload still cost-free (CLAUDE.md rule 2). Unit
coverage for argument validation and the cost-containment whitelist lives
in tests/unit/test_llm_dispatch.py; this file is what proves the real
services/pricing and services/inventory code underneath actually agrees
with dispatch.py's expectations.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from typing import Any

import psycopg
import pytest

from services.agent.llm.dispatch import (
    QUOTE_RESULT_KEYS,
    dispatch_check_availability,
    dispatch_get_quote,
    dispatch_tool,
)
from tests.integration._seed import (
    flat_demand_curve,
    flat_min_profit,
    seed_allotment_nights,
    seed_conversation,
    seed_hotel_and_room_type,
    seed_price_rule,
    seed_season,
)

pytestmark = pytest.mark.usefixtures("db_conn")

_NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _seed_default_season(conn: psycopg.Connection[Any]) -> None:
    seed_season(
        conn,
        season_name="Default",
        calendar_type="gregorian",
        start_month=1,
        start_day=1,
        end_month=1,
        end_day=1,
        priority=0,
        is_default=True,
    )


def _seed_priceable_stay(conn: psycopg.Connection[Any]) -> tuple[int, int]:
    hotel_id, room_type_id = seed_hotel_and_room_type(conn)
    _seed_default_season(conn)
    seed_allotment_nights(
        conn,
        hotel_id,
        room_type_id,
        date(2026, 9, 10),
        nights=2,
        total_rooms=5,
        cost_per_night=10_000,
    )
    seed_price_rule(
        conn,
        scope="global",
        target_margin_bps=2_000,
        min_profit_by_lead_time=flat_min_profit(1_000),
        demand_curve=flat_demand_curve(),
    )
    return hotel_id, room_type_id


def test_get_quote_dispatch_creates_a_real_quote_and_returns_no_cost_fields(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = _seed_priceable_stay(db_conn)
    args = {
        "hotel_id": hotel_id,
        "room_type_id": room_type_id,
        "check_in": "2026-09-10",
        "check_out": "2026-09-12",
        "rooms": 1,
    }

    result = dispatch_get_quote(
        db_conn, args, now=_NOW, customer_phone="+966500000001", conversation_id=None
    )

    assert result.keys() == QUOTE_RESULT_KEYS
    assert result["priced"] is True

    row = db_conn.execute(
        "SELECT hotel_id, room_type_id, customer_phone FROM quotes WHERE id = %s",
        (result["quote_id"],),
    ).fetchone()
    assert row == (hotel_id, room_type_id, "+966500000001")

    serialized = repr(result)
    assert "cost_per_night" not in serialized
    assert "10000" not in serialized  # the seeded cost_per_night, in halalas


def test_get_quote_dispatch_reports_unpriced_when_no_allotment_exists(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    _seed_default_season(db_conn)
    args = {
        "hotel_id": hotel_id,
        "room_type_id": room_type_id,
        "check_in": "2026-09-10",
        "check_out": "2026-09-11",
        "rooms": 1,
    }

    result = dispatch_get_quote(
        db_conn, args, now=_NOW, customer_phone=None, conversation_id=None
    )

    assert result == {
        "priced": False,
        "reason": "no_allotment_for_dates",
        "hotel_id": hotel_id,
        "room_type_id": room_type_id,
        "check_in": "2026-09-10",
        "check_out": "2026-09-11",
    }


def test_get_quote_dispatch_reports_unpriced_despite_a_valid_price_rule(
    db_conn: psycopg.Connection[Any],
) -> None:
    """A fully resolvable global price rule must not change the outcome —
    the missing-allotment check has to fire on its own, not as a side
    effect of price-rule resolution failing first (the exact ordering
    bug this test guards against: see dispatch.py's module docstring).
    """
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    _seed_default_season(db_conn)
    seed_price_rule(
        db_conn,
        scope="global",
        target_margin_bps=2_000,
        min_profit_by_lead_time=flat_min_profit(1_000),
        demand_curve=flat_demand_curve(),
    )
    args = {
        "hotel_id": hotel_id,
        "room_type_id": room_type_id,
        "check_in": "2026-09-10",
        "check_out": "2026-09-11",
        "rooms": 1,
    }

    result = dispatch_get_quote(
        db_conn, args, now=_NOW, customer_phone=None, conversation_id=None
    )

    assert result == {
        "priced": False,
        "reason": "no_allotment_for_dates",
        "hotel_id": hotel_id,
        "room_type_id": room_type_id,
        "check_in": "2026-09-10",
        "check_out": "2026-09-11",
    }


def test_check_availability_dispatch_reflects_real_inventory(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = _seed_priceable_stay(db_conn)
    args = {
        "hotel_id": hotel_id,
        "room_type_id": room_type_id,
        "check_in": "2026-09-10",
        "check_out": "2026-09-12",
        "rooms": 5,
    }

    assert dispatch_check_availability(db_conn, args)["available"] is True

    args["rooms"] = 6
    assert dispatch_check_availability(db_conn, args)["available"] is False


def test_dispatch_tool_logs_get_quote_result_with_no_cost_fields(
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The end-to-end proof for CLAUDE.md rule 2 applied to the tool-call
    log too: a real Quote priced against real (seeded, cost-bearing)
    allotments must still log only priced/quote_id -- never the
    cost_per_night, target_margin_bps or min_profit_halalas that
    compute_quote's own audit trail carries.
    """
    caplog.set_level(logging.INFO, logger="services.agent.llm.dispatch")
    hotel_id, room_type_id = _seed_priceable_stay(db_conn)
    conversation_id = seed_conversation(db_conn)
    args = {
        "hotel_id": hotel_id,
        "room_type_id": room_type_id,
        "check_in": "2026-09-10",
        "check_out": "2026-09-12",
        "rooms": 1,
    }

    result = dispatch_tool(
        db_conn,
        "get_quote",
        args,
        now=_NOW,
        customer_phone="+966500000001",
        conversation_id=conversation_id,
        resolved_stays={(hotel_id, room_type_id)},
    )

    records = [json.loads(r.getMessage()) for r in caplog.records]
    assert len(records) == 1
    record = records[0]
    assert record["event"] == "agent_tool_call"
    assert record["conversation_id"] == conversation_id
    assert record["tool_name"] == "get_quote"
    assert record["arguments"] == args
    assert record["result_summary"] == {"priced": True, "quote_id": result["quote_id"]}
    assert record["error_type"] is None

    serialized = json.dumps(record)
    for forbidden in ("cost_per_night", "10000", "target_margin", "min_profit"):
        assert forbidden not in serialized


def test_dispatch_tool_logs_check_availability_result(
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.agent.llm.dispatch")
    hotel_id, room_type_id = _seed_priceable_stay(db_conn)
    args = {
        "hotel_id": hotel_id,
        "room_type_id": room_type_id,
        "check_in": "2026-09-10",
        "check_out": "2026-09-12",
        "rooms": 6,
    }

    dispatch_tool(
        db_conn,
        "check_availability",
        args,
        now=_NOW,
        customer_phone=None,
        conversation_id=None,
        resolved_stays={(hotel_id, room_type_id)},
    )

    records = [json.loads(r.getMessage()) for r in caplog.records]
    assert len(records) == 1
    assert records[0]["tool_name"] == "check_availability"
    assert records[0]["result_summary"] == {"available": False}
    assert records[0]["error_type"] is None
