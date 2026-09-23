"""Integration tests for services/agent/llm/session.py against a real
Postgres: where a session starts, that the model's window never crosses an
idle gap, that the per-session counters reset exactly when they should, and
that last_message_at moves. Every message is placed at an explicit time so
no test depends on how long it takes to run.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from services.agent.llm.config import SESSION_IDLE_GAP
from services.agent.llm.context import load_recent_messages
from services.agent.llm.session import (
    load_session_start,
    start_new_session_if_idle,
    touch_last_message_at,
)
from tests.integration._seed import (
    seed_conversation,
    seed_hotel_and_room_type,
    seed_message,
    seed_quote,
)

pytestmark = pytest.mark.usefixtures("db_conn")

_BASE = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
_JUST_OVER = SESSION_IDLE_GAP + timedelta(seconds=1)


def _message_at(
    conn: psycopg.Connection[Any], conversation_id: int, at: datetime, body: str = "m"
) -> None:
    seed_message(conn, conversation_id, direction="inbound", body=body, created_at=at)


def _counters(
    conn: psycopg.Connection[Any], conversation_id: int
) -> tuple[int, int | None, int]:
    row = conn.execute(
        "SELECT turn_count, active_quote_id, concession_count "
        "FROM conversations WHERE id = %s",
        (conversation_id,),
    ).fetchone()
    assert row is not None
    return (int(row[0]), row[1], int(row[2]))


def _dirty_conversation(conn: psycopg.Connection[Any]) -> int:
    """A conversation whose per-session counters are all non-zero."""
    hotel_id, room_type_id = seed_hotel_and_room_type(conn)
    conversation_id = seed_conversation(conn, turn_count=7)
    quote_id = seed_quote(conn, hotel_id, room_type_id, conversation_id=conversation_id)
    conn.execute(
        "UPDATE conversations SET active_quote_id = %s, concession_count = 2 "
        "WHERE id = %s",
        (quote_id, conversation_id),
    )
    return conversation_id


def test_a_conversation_with_no_messages_has_no_session(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn)

    assert load_session_start(db_conn, conversation_id) is None


def test_with_no_gaps_the_session_starts_at_the_first_message(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn)
    for hours in (0, 2, 5, 9):
        _message_at(db_conn, conversation_id, _BASE + timedelta(hours=hours))

    assert load_session_start(db_conn, conversation_id) == _BASE


def test_a_gap_of_exactly_the_idle_gap_does_not_split_the_session(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn)
    _message_at(db_conn, conversation_id, _BASE)
    _message_at(db_conn, conversation_id, _BASE + SESSION_IDLE_GAP)

    assert load_session_start(db_conn, conversation_id) == _BASE


def test_a_gap_just_over_the_idle_gap_starts_a_new_session(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn)
    _message_at(db_conn, conversation_id, _BASE)
    second = _BASE + _JUST_OVER
    _message_at(db_conn, conversation_id, second)

    assert load_session_start(db_conn, conversation_id) == second


def test_the_latest_session_wins_when_there_are_several_gaps(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn)
    _message_at(db_conn, conversation_id, _BASE)
    _message_at(db_conn, conversation_id, _BASE + timedelta(days=1))
    third_start = _BASE + timedelta(days=2)
    _message_at(db_conn, conversation_id, third_start)
    _message_at(db_conn, conversation_id, third_start + timedelta(hours=1))

    assert load_session_start(db_conn, conversation_id) == third_start


def test_sessions_are_per_conversation(db_conn: psycopg.Connection[Any]) -> None:
    quiet = seed_conversation(db_conn, customer_phone="+966500000010")
    busy = seed_conversation(db_conn, customer_phone="+966500000011")
    _message_at(db_conn, quiet, _BASE)
    _message_at(db_conn, busy, _BASE + timedelta(days=3))

    assert load_session_start(db_conn, quiet) == _BASE


def test_the_window_never_reaches_back_across_an_idle_gap(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn)
    for index in range(4):
        _message_at(
            db_conn,
            conversation_id,
            _BASE + timedelta(minutes=index),
            body=f"yesterday {index}",
        )
    today = _BASE + timedelta(days=1)
    _message_at(db_conn, conversation_id, today, body="hello")

    messages = load_recent_messages(db_conn, conversation_id, limit=10)

    assert [m.body for m in messages] == ["hello"]


def test_the_window_is_still_capped_at_the_limit_inside_one_session(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn)
    for index in range(12):
        _message_at(
            db_conn,
            conversation_id,
            _BASE + timedelta(minutes=index),
            body=f"message {index}",
        )

    messages = load_recent_messages(db_conn, conversation_id, limit=10)

    assert [m.body for m in messages] == [f"message {i}" for i in range(2, 12)]


def test_a_conversation_with_no_messages_has_an_empty_window(
    db_conn: psycopg.Connection[Any],
) -> None:
    assert load_recent_messages(db_conn, seed_conversation(db_conn), limit=10) == []


def test_an_idle_conversation_has_its_counters_reset(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = _dirty_conversation(db_conn)
    _message_at(db_conn, conversation_id, _BASE)

    was_reset = start_new_session_if_idle(
        db_conn, conversation_id=conversation_id, now=_BASE + _JUST_OVER
    )

    assert was_reset is True
    assert _counters(db_conn, conversation_id) == (0, None, 0)


def test_a_conversation_active_within_the_idle_gap_keeps_its_counters(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = _dirty_conversation(db_conn)
    before = _counters(db_conn, conversation_id)
    _message_at(db_conn, conversation_id, _BASE)

    was_reset = start_new_session_if_idle(
        db_conn, conversation_id=conversation_id, now=_BASE + timedelta(hours=5)
    )

    assert was_reset is False
    assert _counters(db_conn, conversation_id) == before


def test_a_last_message_exactly_the_idle_gap_old_is_not_yet_idle(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Matches load_session_start, where a gap of exactly the idle gap does
    not split a session: the reset and the window must agree on the edge."""
    conversation_id = _dirty_conversation(db_conn)
    before = _counters(db_conn, conversation_id)
    _message_at(db_conn, conversation_id, _BASE)

    was_reset = start_new_session_if_idle(
        db_conn, conversation_id=conversation_id, now=_BASE + SESSION_IDLE_GAP
    )

    assert was_reset is False
    assert _counters(db_conn, conversation_id) == before


def test_a_conversation_with_no_messages_is_never_reset(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = _dirty_conversation(db_conn)
    before = _counters(db_conn, conversation_id)

    was_reset = start_new_session_if_idle(
        db_conn, conversation_id=conversation_id, now=_BASE + timedelta(days=30)
    )

    assert was_reset is False
    assert _counters(db_conn, conversation_id) == before


def test_already_zeroed_counters_are_left_untouched(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn)
    _message_at(db_conn, conversation_id, _BASE)

    was_reset = start_new_session_if_idle(
        db_conn, conversation_id=conversation_id, now=_BASE + timedelta(days=1)
    )

    assert was_reset is False


def test_a_reset_is_logged_as_a_structured_event(
    db_conn: psycopg.Connection[Any], caplog: pytest.LogCaptureFixture
) -> None:
    conversation_id = _dirty_conversation(db_conn)
    _message_at(db_conn, conversation_id, _BASE)
    caplog.set_level(logging.INFO, logger="services.agent.llm.session")

    start_new_session_if_idle(
        db_conn, conversation_id=conversation_id, now=_BASE + timedelta(days=1)
    )

    events = [json.loads(r.getMessage()) for r in caplog.records]
    assert events == [
        {"event": "conversation_session_reset", "conversation_id": conversation_id}
    ]


def test_touching_last_message_at_moves_it_to_now(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn)
    db_conn.execute(
        "UPDATE conversations SET last_message_at = %s WHERE id = %s",
        (_BASE, conversation_id),
    )

    touch_last_message_at(db_conn, conversation_id=conversation_id)

    row = db_conn.execute(
        "SELECT last_message_at FROM conversations WHERE id = %s", (conversation_id,)
    ).fetchone()
    assert row is not None
    assert abs(datetime.now(UTC) - row[0]) < timedelta(seconds=30)
