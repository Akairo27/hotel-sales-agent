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
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest

from services.agent.llm.dispatch import (
    QUOTE_RESULT_KEYS,
    dispatch_check_availability,
    dispatch_get_quote,
    dispatch_tool,
)
from services.agent.llm.errors import InvalidToolArgumentsError
from tests.integration._seed import (
    flat_demand_curve,
    flat_min_profit,
    returning_id,
    seed_actor,
    seed_allotment_night,
    seed_allotment_nights,
    seed_conversation,
    seed_hotel_and_room_type,
    seed_price_override,
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
    assert record["result_summary"] == {
        "priced": True,
        "quote_id": result["quote_id"],
        "reason": None,
    }
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


# --- get_quote's availability gate --------------------------------------------

_STAY_START = date(2026, 9, 10)


def _seed_stay_with_inventory(
    conn: psycopg.Connection[Any],
    nights: list[tuple[int, int, int]],
    *,
    start: date = _STAY_START,
) -> tuple[int, int]:
    """A fully priceable stay starting `start` (_STAY_START unless a test
    needs another date), one night per (total, reserved, held) entry --
    everything pricing needs is in place, so any unpriced result from these
    tests is the availability gate's doing and nothing else."""
    hotel_id, room_type_id = seed_hotel_and_room_type(conn)
    _seed_default_season(conn)
    for offset, (total, reserved, held) in enumerate(nights):
        seed_allotment_night(
            conn,
            hotel_id,
            room_type_id,
            start + timedelta(days=offset),
            total_rooms=total,
            reserved=reserved,
            held=held,
        )
    seed_price_rule(
        conn,
        scope="global",
        target_margin_bps=2_000,
        min_profit_by_lead_time=flat_min_profit(1_000),
        demand_curve=flat_demand_curve(),
    )
    return hotel_id, room_type_id


def _stay_args(
    hotel_id: int,
    room_type_id: int,
    *,
    nights: int,
    rooms: int,
    start: date = _STAY_START,
) -> dict[str, Any]:
    return {
        "hotel_id": hotel_id,
        "room_type_id": room_type_id,
        "check_in": start.isoformat(),
        "check_out": (start + timedelta(days=nights)).isoformat(),
        "rooms": rooms,
    }


def _insufficient_availability_result(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "priced": False,
        "reason": "insufficient_availability",
        "hotel_id": args["hotel_id"],
        "room_type_id": args["room_type_id"],
        "check_in": args["check_in"],
        "check_out": args["check_out"],
    }


def _quote_row_count(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute("SELECT count(*) FROM quotes").fetchone()
    assert row is not None
    return int(row[0])


def test_get_quote_dispatch_prices_when_free_rooms_exactly_equal_the_rooms_requested(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The boundary of the gate: 2 rooms free on each night (night one has
    3 reserved, night two has 2 reserved + 1 held) and 2 rooms requested is
    still sellable."""
    hotel_id, room_type_id = _seed_stay_with_inventory(db_conn, [(5, 3, 0), (5, 2, 1)])
    args = _stay_args(hotel_id, room_type_id, nights=2, rooms=2)

    result = dispatch_get_quote(
        db_conn, args, now=_NOW, customer_phone=None, conversation_id=None
    )

    assert result["priced"] is True
    assert _quote_row_count(db_conn) == 1


@pytest.mark.parametrize(
    "inventory",
    [
        pytest.param([(5, 5, 0)], id="sold-out-by-reserved"),
        pytest.param([(5, 0, 5)], id="sold-out-by-held"),
        pytest.param([(5, 3, 2)], id="sold-out-by-reserved-and-held"),
        pytest.param([(0, 0, 0)], id="zero-total"),
    ],
)
def test_get_quote_dispatch_declines_a_night_with_no_free_rooms(
    db_conn: psycopg.Connection[Any], inventory: list[tuple[int, int, int]]
) -> None:
    """Before the gate, occupancy of exactly 1.0 (every one of these nights)
    fell outside the demand curve's [0, 1) band and raised
    NoMatchingBandError -- a turn that sent the customer nothing. Now it is
    an ordinary unpriced result, with no `quotes` row and no room count in
    it (the exact-dict comparison is what proves the latter)."""
    hotel_id, room_type_id = _seed_stay_with_inventory(db_conn, inventory)
    args = _stay_args(hotel_id, room_type_id, nights=1, rooms=1)

    result = dispatch_get_quote(
        db_conn, args, now=_NOW, customer_phone=None, conversation_id=None
    )

    assert result == _insufficient_availability_result(args)
    assert _quote_row_count(db_conn) == 0


def test_get_quote_dispatch_declines_when_more_rooms_are_requested_than_are_free(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The partial case that used to be priced anyway: 2 rooms free, 3
    requested."""
    hotel_id, room_type_id = _seed_stay_with_inventory(db_conn, [(5, 3, 0)])
    args = _stay_args(hotel_id, room_type_id, nights=1, rooms=3)

    result = dispatch_get_quote(
        db_conn, args, now=_NOW, customer_phone=None, conversation_id=None
    )

    assert result == _insufficient_availability_result(args)
    assert _quote_row_count(db_conn) == 0


def test_get_quote_dispatch_declines_when_only_one_night_of_the_stay_is_short(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Every night must cover the request, not just the first: night one
    has 5 free, night two has 1, and 2 rooms are requested."""
    hotel_id, room_type_id = _seed_stay_with_inventory(db_conn, [(5, 0, 0), (5, 4, 0)])
    args = _stay_args(hotel_id, room_type_id, nights=2, rooms=2)

    result = dispatch_get_quote(
        db_conn, args, now=_NOW, customer_phone=None, conversation_id=None
    )

    assert result == _insufficient_availability_result(args)
    assert _quote_row_count(db_conn) == 0


_PAST_STAY_START = date(2026, 8, 20)  # before _NOW (2026-09-01)


@pytest.mark.parametrize(
    "inventory",
    [
        pytest.param([(5, 5, 0)], id="sold-out-night"),
        pytest.param([(5, 0, 0)], id="free-night"),
    ],
)
def test_get_quote_dispatch_rejects_a_past_check_in_whatever_the_inventory_holds(
    db_conn: psycopg.Connection[Any], inventory: list[tuple[int, int, int]]
) -> None:
    """A past check_in is a date problem, never an inventory outcome: the
    sold-out night and the free night must give the identical
    InvalidToolArgumentsError, and neither may write a `quotes` row. The
    sold-out case is the one that would have been reported as unpriced
    (insufficient_availability) if the inventory checks ran before date
    validation."""
    hotel_id, room_type_id = _seed_stay_with_inventory(
        db_conn, inventory, start=_PAST_STAY_START
    )
    args = _stay_args(hotel_id, room_type_id, nights=1, rooms=1, start=_PAST_STAY_START)

    with pytest.raises(InvalidToolArgumentsError) as exc_info:
        dispatch_get_quote(
            db_conn, args, now=_NOW, customer_phone=None, conversation_id=None
        )

    assert str(exc_info.value) == "check_in must not be in the past"
    assert _quote_row_count(db_conn) == 0


def test_get_quote_dispatch_reports_an_allotment_without_inventory_as_no_allotment(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The trial database holds a night with an allotments row and no
    room_night_inventory row (migration 0029's comment). compute_quote used
    to report it as no_allotment_for_dates; check_availability returns
    False for it too, so without the coverage check also requiring the
    inventory row it would be reported as insufficient_availability -- a
    night with no inventory is missing inventory, not sold out. Everything
    else pricing needs is in place, so only that distinction is under
    test."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    _seed_default_season(db_conn)
    seed_actor(db_conn)
    returning_id(
        db_conn,
        "INSERT INTO allotments (hotel_id, room_type_id, stay_date, total_rooms, "
        "cost_per_night) VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (hotel_id, room_type_id, _STAY_START, 5, 10_000),
    )
    seed_price_rule(
        db_conn,
        scope="global",
        target_margin_bps=2_000,
        min_profit_by_lead_time=flat_min_profit(1_000),
        demand_curve=flat_demand_curve(),
    )
    args = _stay_args(hotel_id, room_type_id, nights=1, rooms=1)

    result = dispatch_get_quote(
        db_conn, args, now=_NOW, customer_phone=None, conversation_id=None
    )

    assert result == {
        "priced": False,
        "reason": "no_allotment_for_dates",
        "hotel_id": hotel_id,
        "room_type_id": room_type_id,
        "check_in": args["check_in"],
        "check_out": args["check_out"],
    }
    assert _quote_row_count(db_conn) == 0


def test_get_quote_dispatch_declines_a_sold_out_night_even_with_an_active_override(
    db_conn: psycopg.Connection[Any],
) -> None:
    """An active price override skips the occupancy read entirely, so
    before the gate a sold-out night carrying one was priced and a quote
    written. An override sets the price, not whether a room exists to
    sell -- the gate sits ahead of compute_quote and declines it too."""
    hotel_id, room_type_id = _seed_stay_with_inventory(db_conn, [(5, 5, 0)])
    seed_price_override(db_conn, hotel_id, room_type_id, _STAY_START)
    args = _stay_args(hotel_id, room_type_id, nights=1, rooms=1)

    result = dispatch_get_quote(
        db_conn, args, now=_NOW, customer_phone=None, conversation_id=None
    )

    assert result == _insufficient_availability_result(args)
    assert _quote_row_count(db_conn) == 0


def test_dispatch_tool_logs_the_reason_for_an_unpriced_get_quote(
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The journal can now tell an availability decline from a missing
    allotment -- both used to log as just {"priced": False,
    "quote_id": None}."""
    caplog.set_level(logging.INFO, logger="services.agent.llm.dispatch")
    hotel_id, room_type_id = _seed_stay_with_inventory(db_conn, [(5, 5, 0)])
    args = _stay_args(hotel_id, room_type_id, nights=1, rooms=1)

    dispatch_tool(
        db_conn,
        "get_quote",
        args,
        now=_NOW,
        customer_phone=None,
        conversation_id=None,
        resolved_stays={(hotel_id, room_type_id)},
    )

    records = [json.loads(r.getMessage()) for r in caplog.records]
    assert len(records) == 1
    assert records[0]["tool_name"] == "get_quote"
    assert records[0]["result_summary"] == {
        "priced": False,
        "quote_id": None,
        "reason": "insufficient_availability",
    }
    assert records[0]["error_type"] is None
