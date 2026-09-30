"""request_booking_follow_up against a real Postgres: its two database
checks, one escalation per quote (migration 0032's partial unique index,
including two answers handled at the same moment), and the path run as
hotel_agent, the role the webhook connects as."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from services.agent.llm.booking_follow_up import request_booking_follow_up
from services.agent.llm.errors import InvalidToolArgumentsError
from tests.integration._seed import (
    seed_conversation,
    seed_hotel_and_room_type,
    seed_message,
    seed_quote,
)

pytestmark = pytest.mark.usefixtures("db_conn")

_PHONE = "+966500000001"
_OTHER_PHONE = "+966500000002"
_SESSION_START = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)
_QUOTED_AT = _SESSION_START + timedelta(minutes=1)
_YES_AT = _SESSION_START + timedelta(minutes=2)
# Past SESSION_IDLE_GAP (6 hours): a message this late opens a new session.
_NEXT_SESSION_AT = _SESSION_START + timedelta(hours=7)
# How long the second answer is given to block on the first one's
# uncommitted row before the test concludes it did not wait at all.
_BLOCK_OBSERVATION_SECONDS = 1.0


def _conversation_with_a_quote(
    db_conn: psycopg.Connection[Any], *, phone: str = _PHONE
) -> tuple[int, int]:
    """A conversation whose session opened at _SESSION_START and was quoted
    a minute later. Returns (conversation_id, quote_id)."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    conversation_id = seed_conversation(db_conn, customer_phone=phone)
    seed_message(
        db_conn,
        conversation_id,
        direction="inbound",
        body="price please",
        customer_phone=phone,
        created_at=_SESSION_START,
    )
    quote_id = seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        customer_phone=phone,
        created_at=_QUOTED_AT,
    )
    return conversation_id, quote_id


def _customer_writes(
    db_conn: psycopg.Connection[Any],
    conversation_id: int,
    *,
    at: datetime = _YES_AT,
    phone: str = _PHONE,
) -> None:
    seed_message(
        db_conn,
        conversation_id,
        direction="inbound",
        body="yes",
        customer_phone=phone,
        created_at=at,
    )


def _booking_requests(db_conn: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return db_conn.execute(
        "SELECT quote_id, customer_phone, notes FROM escalations "
        "WHERE reason = 'booking_requested' ORDER BY id"
    ).fetchall()


def test_a_yes_after_the_quote_opens_one_booking_request(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, quote_id = _conversation_with_a_quote(db_conn)
    _customer_writes(db_conn, conversation_id)

    result = request_booking_follow_up(
        db_conn, quote_id=quote_id, conversation_id=conversation_id
    )

    assert result == {
        "requested": True,
        "quote_id": quote_id,
        "already_requested": False,
    }
    assert _booking_requests(db_conn) == [(quote_id, _PHONE, "{}")]


def test_saying_yes_twice_opens_only_one_request(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, quote_id = _conversation_with_a_quote(db_conn)
    _customer_writes(db_conn, conversation_id)
    request_booking_follow_up(
        db_conn, quote_id=quote_id, conversation_id=conversation_id
    )
    _customer_writes(db_conn, conversation_id, at=_YES_AT + timedelta(minutes=1))

    again = request_booking_follow_up(
        db_conn, quote_id=quote_id, conversation_id=conversation_id
    )

    assert again["already_requested"] is True
    assert len(_booking_requests(db_conn)) == 1


def test_each_quote_gets_its_own_request(db_conn: psycopg.Connection[Any]) -> None:
    """One per quote, not one per conversation: a second stay the customer
    also says yes to is a second booking."""
    conversation_id, first_quote_id = _conversation_with_a_quote(db_conn)
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    second_quote_id = seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        customer_phone=_PHONE,
        created_at=_QUOTED_AT,
    )
    _customer_writes(db_conn, conversation_id)

    for quote_id in (first_quote_id, second_quote_id):
        request_booking_follow_up(
            db_conn, quote_id=quote_id, conversation_id=conversation_id
        )

    assert [row[0] for row in _booking_requests(db_conn)] == [
        first_quote_id,
        second_quote_id,
    ]


def test_no_message_since_the_quote_is_not_confirmable(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The tool answers a customer's message about a price they have seen,
    never the same turn that produced the price."""
    conversation_id, quote_id = _conversation_with_a_quote(db_conn)

    with pytest.raises(InvalidToolArgumentsError) as exc_info:
        request_booking_follow_up(
            db_conn, quote_id=quote_id, conversation_id=conversation_id
        )

    assert exc_info.value.code == "quote_not_confirmable"
    assert _booking_requests(db_conn) == []


def test_a_quote_from_another_conversation_is_not_confirmable(
    db_conn: psycopg.Connection[Any],
) -> None:
    _, other_quote_id = _conversation_with_a_quote(db_conn, phone=_OTHER_PHONE)
    conversation_id, _ = _conversation_with_a_quote(db_conn)
    _customer_writes(db_conn, conversation_id)

    with pytest.raises(InvalidToolArgumentsError) as exc_info:
        request_booking_follow_up(
            db_conn, quote_id=other_quote_id, conversation_id=conversation_id
        )

    assert exc_info.value.code == "quote_not_confirmable"
    assert _booking_requests(db_conn) == []


def test_a_quote_from_an_earlier_session_is_not_confirmable(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The same session scoping the output guard uses: a price from a
    session that has ended is not what the customer is saying yes to."""
    conversation_id, quote_id = _conversation_with_a_quote(db_conn)
    _customer_writes(db_conn, conversation_id, at=_NEXT_SESSION_AT)

    with pytest.raises(InvalidToolArgumentsError) as exc_info:
        request_booking_follow_up(
            db_conn, quote_id=quote_id, conversation_id=conversation_id
        )

    assert exc_info.value.code == "quote_not_confirmable"


def test_runs_as_hotel_agent(
    db_conn: psycopg.Connection[Any], agent_database_url: str
) -> None:
    """Migration 0032's column grants are all the insert and its conflict
    check need, as the role the webhook really connects as."""
    conversation_id, quote_id = _conversation_with_a_quote(db_conn)
    _customer_writes(db_conn, conversation_id)

    with psycopg.connect(agent_database_url, autocommit=True) as agent:
        first = request_booking_follow_up(
            agent, quote_id=quote_id, conversation_id=conversation_id
        )
        second = request_booking_follow_up(
            agent, quote_id=quote_id, conversation_id=conversation_id
        )

    assert (first["already_requested"], second["already_requested"]) == (False, True)
    assert _booking_requests(db_conn) == [(quote_id, _PHONE, "{}")]


def test_two_answers_at_the_same_moment_open_one_request(
    db_conn: psycopg.Connection[Any], agent_database_url: str
) -> None:
    """The race a check-then-insert would lose: the first answer's row is
    not committed yet when the second arrives. The unique index makes the
    second wait for the first, then do nothing."""
    conversation_id, quote_id = _conversation_with_a_quote(db_conn)
    _customer_writes(db_conn, conversation_id)
    second_result: list[dict[str, Any]] = []

    def _second_answer() -> None:
        with psycopg.connect(agent_database_url, autocommit=True) as second:
            second_result.append(
                request_booking_follow_up(
                    second, quote_id=quote_id, conversation_id=conversation_id
                )
            )

    with psycopg.connect(agent_database_url) as first:
        first_result = request_booking_follow_up(
            first, quote_id=quote_id, conversation_id=conversation_id
        )
        racer = threading.Thread(target=_second_answer)
        racer.start()
        racer.join(timeout=_BLOCK_OBSERVATION_SECONDS)
        assert racer.is_alive(), "the second answer did not wait for the first"
        first.commit()
    racer.join()

    assert first_result["already_requested"] is False
    assert second_result == [
        {"requested": True, "quote_id": quote_id, "already_requested": True}
    ]
    assert len(_booking_requests(db_conn)) == 1
