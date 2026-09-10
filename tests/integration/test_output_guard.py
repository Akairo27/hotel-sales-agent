"""Integration tests for services/agent/output_guard against a real
Postgres database — the DB-backed half of the guard's contract. Pure
matching/extraction logic is covered without a database in
tests/unit/test_output_guard_extraction.py and
tests/unit/test_output_guard_decision.py; the required CLAUDE.md §6
adversarial corpus lives in tests/adversarial/test_output_guard.py.
"""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import UTC, date, datetime
from typing import Any

import psycopg
import pytest

from lib.money import format_halalas_as_sar
from services.agent.llm.dispatch import dispatch_get_quote
from services.agent.llm.errors import ConversationNotFoundError
from services.agent.output_guard.enforcement import (
    REASON_FOREIGN_CURRENCY,
    REASON_MISMATCH,
    REASON_MISSING_CURRENCY,
    REASON_UNPARSEABLE,
    enforce_outbound_text,
)
from services.agent.output_guard.quotes import load_allowed_amounts
from tests.integration._seed import (
    flat_demand_curve,
    flat_min_profit,
    seed_allotment_nights,
    seed_conversation,
    seed_hotel_and_room_type,
    seed_price_rule,
    seed_quote,
    seed_season,
)

pytestmark = pytest.mark.usefixtures("db_conn")

_NOW = datetime(2026, 9, 1, tzinfo=UTC)

# A cost-bearing quotes.nights shape (override_applied=false) with
# distinctive, non-colliding sentinel values, so a substring check for
# any of them cannot be accidentally satisfied by a legitimate
# ask/min_allowed/total value.
_NIGHTS_WITH_COST_FIELDS = json.dumps(
    [
        {
            "date": "2026-09-01",
            "season_id": 1,
            "ask": 45_000,
            "min_allowed": 30_000,
            "override_applied": False,
            "cost_per_night": 77_777,
            "occupancy": 0.5,
            "target_margin_bps": 8_811,
            "target_margin_rule_id": 1,
            "price_after_margin": 66_622,
            "occupancy_multiplier_bps": 100,
            "lead_time_multiplier_bps": 100,
            "demand_factor_bps": 100,
            "demand_curve_rule_id": 1,
            "min_profit_halalas": 99_133,
            "min_profit_rule_id": 1,
        },
        {
            "date": "2026-09-02",
            "season_id": 1,
            "ask": 45_000,
            "min_allowed": 30_000,
            "override_applied": True,
        },
    ]
)


def _seed_priceable_stay(conn: psycopg.Connection[Any]) -> tuple[int, int]:
    hotel_id, room_type_id = seed_hotel_and_room_type(conn)
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


def test_load_allowed_amounts_returns_every_quote_in_the_conversation(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    conversation_id = seed_conversation(db_conn)
    seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        ask_price_total=135_000,
        min_allowed_total=90_000,
    )
    seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        ask_price_total=300_000,
        min_allowed_total=200_000,
    )

    result = load_allowed_amounts(db_conn, conversation_id)

    assert len(result.quote_ids) == 2
    assert {135_000, 300_000, 20_000}.issubset(result.amounts_halalas)
    assert result.floor_halalas == 10_000  # the seeded nights' min_allowed


def test_load_allowed_amounts_ignores_quotes_from_another_conversation(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    this_conversation = seed_conversation(db_conn)
    other_conversation = seed_conversation(db_conn)
    seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=other_conversation,
        ask_price_total=999_999,
        min_allowed_total=999_999,
    )

    result = load_allowed_amounts(db_conn, this_conversation)

    assert result.quote_ids == ()
    assert result.amounts_halalas == frozenset()
    assert result.floor_halalas is None


def test_load_allowed_amounts_ignores_quotes_with_a_null_conversation_id(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    conversation_id = seed_conversation(db_conn)
    seed_quote(db_conn, hotel_id, room_type_id, conversation_id=None)

    result = load_allowed_amounts(db_conn, conversation_id)

    assert result.amounts_halalas == frozenset()


def test_load_allowed_amounts_never_surfaces_a_cost_bearing_field(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    conversation_id = seed_conversation(db_conn)
    seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        ask_price_total=90_000,
        min_allowed_total=60_000,
        nights=_NIGHTS_WITH_COST_FIELDS,
    )

    result = load_allowed_amounts(db_conn, conversation_id)

    assert {f.name for f in fields(result)} == {
        "quote_ids",
        "amounts_halalas",
        "floor_halalas",
    }
    serialized = repr(result)
    for cost_value in ("77777", "8811", "66622", "99133"):
        assert cost_value not in serialized


def test_a_reply_quoting_the_real_total_passes_and_opens_no_escalation(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    conversation_id = seed_conversation(db_conn)
    seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        ask_price_total=135_000,
        min_allowed_total=90_000,
    )

    verdict = enforce_outbound_text(
        db_conn, conversation_id=conversation_id, text="Your total is 1,350.00 SAR"
    )

    assert verdict.allowed is True
    assert verdict.escalation_id is None
    count = db_conn.execute("SELECT count(*) FROM escalations").fetchone()
    assert count == (0,)


def test_a_blocked_reply_opens_exactly_one_escalation_row(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    conversation_id = seed_conversation(db_conn, customer_phone="+966522222222")
    seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        ask_price_total=135_000,
        min_allowed_total=90_000,
    )

    verdict = enforce_outbound_text(
        db_conn, conversation_id=conversation_id, text="I can do 100.00 SAR for you"
    )

    assert verdict.allowed is False
    row = db_conn.execute(
        "SELECT conversation_id, customer_phone, reason, resolved_at "
        "FROM escalations WHERE id = %s",
        (verdict.escalation_id,),
    ).fetchone()
    assert row == (conversation_id, "+966522222222", REASON_MISMATCH, None)


def test_the_escalation_notes_carry_the_offending_amount_and_quote_ids(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    conversation_id = seed_conversation(db_conn)
    quote_id = seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        ask_price_total=135_000,
        min_allowed_total=90_000,
    )

    verdict = enforce_outbound_text(
        db_conn, conversation_id=conversation_id, text="I can do 50.00 SAR for you"
    )

    row = db_conn.execute(
        "SELECT notes FROM escalations WHERE id = %s", (verdict.escalation_id,)
    ).fetchone()
    assert row is not None
    notes = json.loads(row[0])
    assert notes["quote_ids"] == [quote_id]
    assert notes["blocked_amounts_halalas"] == [5_000]
    assert notes["reasons"] == ["below_floor"]
    assert notes["blocked_reply_text"] == "I can do 50.00 SAR for you"


def test_the_escalation_notes_never_contain_the_customer_phone_number(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    phone = "+966533333333"
    conversation_id = seed_conversation(db_conn, customer_phone=phone)
    seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        ask_price_total=135_000,
        min_allowed_total=90_000,
    )

    verdict = enforce_outbound_text(
        db_conn, conversation_id=conversation_id, text="I can do 100.00 SAR for you"
    )

    row = db_conn.execute(
        "SELECT notes FROM escalations WHERE id = %s", (verdict.escalation_id,)
    ).fetchone()
    assert row is not None
    assert phone not in row[0]


def test_an_unparseable_blocked_reply_gets_the_unparseable_reason(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    conversation_id = seed_conversation(db_conn)
    seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        ask_price_total=135_000,
        min_allowed_total=90_000,
    )

    verdict = enforce_outbound_text(
        db_conn, conversation_id=conversation_id, text="It comes to 1,2,3.4.5 SAR"
    )

    reason = db_conn.execute(
        "SELECT reason FROM escalations WHERE id = %s", (verdict.escalation_id,)
    ).fetchone()
    assert reason == (REASON_UNPARSEABLE,)


def test_a_foreign_labelled_real_total_is_blocked_with_the_foreign_reason(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The real total, relabelled in a foreign currency, must block end
    to end against a real database — not merely pass extraction/decision
    in isolation."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    conversation_id = seed_conversation(db_conn)
    seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        ask_price_total=135_000,
        min_allowed_total=90_000,
    )

    verdict = enforce_outbound_text(
        db_conn, conversation_id=conversation_id, text="Your total is 1,350.00 USD"
    )

    assert verdict.allowed is False
    reason = db_conn.execute(
        "SELECT reason FROM escalations WHERE id = %s", (verdict.escalation_id,)
    ).fetchone()
    assert reason == (REASON_FOREIGN_CURRENCY,)


def test_a_real_total_with_no_currency_marker_is_blocked_with_the_missing_reason(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The real total with no currency word at all — closing this is
    what makes the deny-list unnecessary to enumerate every unlisted
    currency, since an unlisted one looks identical to no label at all."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    conversation_id = seed_conversation(db_conn)
    seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        ask_price_total=135_000,
        min_allowed_total=90_000,
    )

    verdict = enforce_outbound_text(
        db_conn, conversation_id=conversation_id, text="Your total is 1,350.00"
    )

    assert verdict.allowed is False
    reason = db_conn.execute(
        "SELECT reason FROM escalations WHERE id = %s", (verdict.escalation_id,)
    ).fetchone()
    assert reason == (REASON_MISSING_CURRENCY,)


def test_a_reply_with_both_a_mismatch_and_an_unparseable_amount_is_reason_mismatch(
    db_conn: psycopg.Connection[Any],
) -> None:
    """A concrete wrong amount is judged the more urgent case whenever a
    reply contains both kinds of finding."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    conversation_id = seed_conversation(db_conn)
    seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        ask_price_total=135_000,
        min_allowed_total=90_000,
    )

    verdict = enforce_outbound_text(
        db_conn,
        conversation_id=conversation_id,
        text="It's either 100.00 SAR or 1,2,3.4.5 SAR",
    )

    reason = db_conn.execute(
        "SELECT reason FROM escalations WHERE id = %s", (verdict.escalation_id,)
    ).fetchone()
    assert reason == (REASON_MISMATCH,)


def test_a_block_verdict_always_carries_the_escalation_id_it_opened(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    conversation_id = seed_conversation(db_conn)
    seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        ask_price_total=135_000,
        min_allowed_total=90_000,
    )

    verdict = enforce_outbound_text(
        db_conn, conversation_id=conversation_id, text="900.00 SAR only"
    )

    assert verdict.allowed is False
    assert verdict.escalation_id is not None
    exists = db_conn.execute(
        "SELECT 1 FROM escalations WHERE id = %s", (verdict.escalation_id,)
    ).fetchone()
    assert exists == (1,)


def test_two_blocked_replies_open_two_escalations(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    conversation_id = seed_conversation(db_conn)
    seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        ask_price_total=135_000,
        min_allowed_total=90_000,
    )

    first = enforce_outbound_text(
        db_conn, conversation_id=conversation_id, text="900.00 SAR"
    )
    second = enforce_outbound_text(
        db_conn, conversation_id=conversation_id, text="800.00 SAR"
    )

    assert first.escalation_id != second.escalation_id
    count = db_conn.execute(
        "SELECT count(*) FROM escalations WHERE conversation_id = %s",
        (conversation_id,),
    ).fetchone()
    assert count == (2,)


def test_enforce_raises_for_an_unknown_conversation_id(
    db_conn: psycopg.Connection[Any],
) -> None:
    with pytest.raises(ConversationNotFoundError, match="999999"):
        enforce_outbound_text(db_conn, conversation_id=999_999, text="100.00 SAR")


def test_a_reply_with_no_amounts_is_allowed_when_the_conversation_has_no_quotes(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn)

    verdict = enforce_outbound_text(
        db_conn, conversation_id=conversation_id, text="Let me check for you."
    )

    assert verdict.allowed is True


def test_a_reply_quoting_a_real_compute_quote_result_is_allowed(
    db_conn: psycopg.Connection[Any],
) -> None:
    """End-to-end: seed a real priceable stay, call the real
    dispatch_get_quote/compute_quote, render its result with the real
    format_halalas_as_sar, and prove the guard agrees — then flip one
    digit and prove it blocks. No hand-written expected numbers anywhere
    in this test; it is what proves the guard and the pricing service
    actually agree.
    """
    hotel_id, room_type_id = _seed_priceable_stay(db_conn)
    conversation_id = seed_conversation(db_conn)
    args = {
        "hotel_id": hotel_id,
        "room_type_id": room_type_id,
        "check_in": "2026-09-10",
        "check_out": "2026-09-12",
        "rooms": 1,
    }

    quote_result = dispatch_get_quote(
        db_conn,
        args,
        now=_NOW,
        customer_phone="+966500000001",
        conversation_id=conversation_id,
    )
    assert quote_result["priced"] is True
    real_total_display = quote_result["total_price_display"]

    allowed_verdict = enforce_outbound_text(
        db_conn,
        conversation_id=conversation_id,
        text=f"Your total for the stay is {real_total_display}.",
    )
    assert allowed_verdict.allowed is True

    real_total_halalas = int(
        real_total_display.replace(",", "").replace(" SAR", "").replace(".", "")
    )
    wrong_display = format_halalas_as_sar(real_total_halalas + 1)
    blocked_verdict = enforce_outbound_text(
        db_conn,
        conversation_id=conversation_id,
        text=f"Your total for the stay is {wrong_display}.",
    )
    assert blocked_verdict.allowed is False
