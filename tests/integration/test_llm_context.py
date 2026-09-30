"""Integration tests for context.py against a real Postgres instance —
the two closed decisions from ARCHITECTURE.md §7 this module exists to
enforce: the last-10-message window, and that the customer's phone
number never reaches the model's context.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest

from lib.hijri import to_hijri
from services.agent.llm.config import DEFAULT_QUOTE_VALIDITY_MINUTES
from services.agent.llm.context import (
    CurrentStay,
    build_contents,
    load_conversation_state,
    load_current_stay,
    load_recent_messages,
)
from services.agent.llm.model_types import ModelTurn, UserTurn
from services.agent.llm.prompt import render_system_instruction
from tests.integration._seed import (
    seed_conversation,
    seed_hotel_and_room_type,
    seed_message,
    seed_quote,
)

pytestmark = pytest.mark.usefixtures("db_conn")

_QUOTE_VALIDITY = timedelta(minutes=DEFAULT_QUOTE_VALIDITY_MINUTES)

_TODAY = date(2026, 9, 23)
_PHONE = "+966500000001"
# Every representation the same phone number could plausibly take —
# structurally impossible for any of these to appear given build_contents
# and render_system_instruction never read customer_phone at all, but the
# test proves that rather than assuming it.
_PHONE_VARIANTS = (_PHONE, _PHONE.lstrip("+"), "0" + _PHONE[4:], _PHONE[4:])


def test_load_recent_messages_returns_only_the_window_oldest_first(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    for i in range(12):
        direction = "inbound" if i % 2 == 0 else "outbound"
        seed_message(db_conn, conversation_id, direction=direction, body=f"message {i}")

    messages = load_recent_messages(db_conn, conversation_id, limit=10)

    assert [m.body for m in messages] == [f"message {i}" for i in range(2, 12)]


def test_build_contents_maps_message_direction_to_model_role(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    seed_message(db_conn, conversation_id, direction="inbound", body="hello")
    seed_message(db_conn, conversation_id, direction="outbound", body="hi there")

    messages = load_recent_messages(db_conn, conversation_id, limit=10)
    turns = build_contents(messages)

    roles = ["user" if isinstance(turn, UserTurn) else "model" for turn in turns]
    assert roles == ["user", "model"]


def test_customer_phone_never_appears_in_any_built_content_or_the_system_instruction(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Exhaustive, not a spot check. Every part of every built Content is
    scanned, plus the rendered system instruction — a leak sitting in the
    one part a sampled assertion happened to skip must still fail this
    test.
    """
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    bodies = [
        "I'd like to check availability for two nights",
        "What is the price for a double room?",
        "Can I get a discount on that?",
        "Thank you, that works for me",
    ]
    for i, body in enumerate(bodies):
        direction = "inbound" if i % 2 == 0 else "outbound"
        seed_message(db_conn, conversation_id, direction=direction, body=body)

    state = load_conversation_state(db_conn, conversation_id)
    assert state.customer_phone == _PHONE

    messages = load_recent_messages(db_conn, conversation_id, limit=10)
    turns = build_contents(messages)

    haystacks: list[str] = [
        render_system_instruction(
            customer_name="Ahmed",
            today=_TODAY,
            today_hijri=to_hijri(_TODAY),
            current_stay=load_current_stay(
                db_conn, conversation_id, quote_validity=_QUOTE_VALIDITY
            ),
        )
    ]
    for turn in turns:
        assert isinstance(turn, UserTurn | ModelTurn)
        assert turn.text is not None
        haystacks.append(turn.text)

    assert len(haystacks) == 1 + len(bodies)  # every part really was scanned
    for haystack in haystacks:
        for variant in _PHONE_VARIANTS:
            assert variant not in haystack


def test_load_current_stay_is_none_before_any_quote(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    seed_message(db_conn, conversation_id, direction="inbound", body="hello")

    assert (
        load_current_stay(db_conn, conversation_id, quote_validity=_QUOTE_VALIDITY)
        is None
    )


def test_load_current_stay_is_the_sessions_latest_quote(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The stay the customer last got a price for, with the hotel and room
    type names the model can say -- the dates changed between the two
    quotes, and the newer ones win."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    now = datetime.now(UTC)
    seed_message(
        db_conn,
        conversation_id,
        direction="inbound",
        body="a room please",
        created_at=now - timedelta(minutes=5),
    )
    seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        created_at=now - timedelta(minutes=2),
        check_in=date(2026, 10, 20),
        check_out=date(2026, 10, 22),
    )
    seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        created_at=now - timedelta(minutes=1),
        check_in=date(2026, 10, 21),
        check_out=date(2026, 10, 24),
        rooms=2,
    )

    assert load_current_stay(
        db_conn, conversation_id, quote_validity=_QUOTE_VALIDITY
    ) == CurrentStay(
        hotel_name="Test Hotel",
        room_type_name="Standard",
        check_in=date(2026, 10, 21),
        check_out=date(2026, 10, 24),
        rooms=2,
        total_price_display="200.00 SAR",
        total_price_display_ar="200.00 ريال",
        valid_until=now - timedelta(minutes=1) + _QUOTE_VALIDITY,
        is_valid=True,
    )


def test_load_current_stay_marks_a_quote_past_its_validity_as_expired(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The stay stays in view after the window, but its price does not:
    the prompt then tells the model to call get_quote again."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    now = datetime.now(UTC)
    seed_message(
        db_conn,
        conversation_id,
        direction="inbound",
        body="a room please",
        created_at=now - timedelta(minutes=50),
    )
    seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        created_at=now - timedelta(minutes=45),
    )

    stay = load_current_stay(db_conn, conversation_id, quote_validity=_QUOTE_VALIDITY)

    assert stay is not None
    assert stay.is_valid is False
    assert stay.valid_until == now - timedelta(minutes=45) + _QUOTE_VALIDITY


def test_load_current_stay_ignores_a_quote_from_an_earlier_session(
    db_conn: psycopg.Connection[Any],
) -> None:
    """A stay priced before an idle gap never comes back as "current"."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    now = datetime.now(UTC)
    earlier = now - timedelta(hours=10)
    seed_message(
        db_conn, conversation_id, direction="inbound", body="then", created_at=earlier
    )
    seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        created_at=earlier + timedelta(minutes=1),
    )
    seed_message(
        db_conn, conversation_id, direction="inbound", body="now", created_at=now
    )

    assert (
        load_current_stay(db_conn, conversation_id, quote_validity=_QUOTE_VALIDITY)
        is None
    )


def test_load_current_stay_is_none_for_a_conversation_without_messages(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    seed_quote(db_conn, hotel_id, room_type_id, conversation_id=conversation_id)

    assert (
        load_current_stay(db_conn, conversation_id, quote_validity=_QUOTE_VALIDITY)
        is None
    )
