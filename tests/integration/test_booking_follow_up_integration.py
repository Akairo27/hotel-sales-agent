"""request_booking_follow_up against a real Postgres: which quote it picks
(the latest still-valid one the customer has written after), what it
refuses, one escalation per quote (migration 0032's partial unique index,
including two answers handled at the same moment), and the path run as
hotel_agent, the role the webhook connects as.

Timestamps are relative to the present: validity is measured against the
database's own now()."""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest

from services.agent.llm.booking_follow_up import (
    BOOKING_FOLLOW_UP_RESULT_KEYS,
    request_booking_follow_up,
)
from services.agent.llm.config import DEFAULT_QUOTE_VALIDITY_MINUTES
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
_VALIDITY = timedelta(minutes=DEFAULT_QUOTE_VALIDITY_MINUTES)
# How long the second answer is given to block on the first one's
# uncommitted row before the test concludes it did not wait at all.
_BLOCK_OBSERVATION_SECONDS = 1.0


def _minutes_ago(minutes: float) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


def _conversation(
    db_conn: psycopg.Connection[Any], *, phone: str = _PHONE, opened: float = 20
) -> int:
    """A conversation whose session opened `opened` minutes ago."""
    conversation_id = seed_conversation(db_conn, customer_phone=phone)
    _customer_writes(db_conn, conversation_id, minutes_ago=opened, phone=phone)
    return conversation_id


def _quote(
    db_conn: psycopg.Connection[Any],
    conversation_id: int,
    *,
    minutes_ago: float,
    phone: str = _PHONE,
    check_in: date = date(2026, 10, 20),
) -> int:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    return seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        customer_phone=phone,
        created_at=_minutes_ago(minutes_ago),
        check_in=check_in,
        check_out=check_in + timedelta(days=2),
    )


def _customer_writes(
    db_conn: psycopg.Connection[Any],
    conversation_id: int,
    *,
    minutes_ago: float,
    phone: str = _PHONE,
) -> None:
    seed_message(
        db_conn,
        conversation_id,
        direction="inbound",
        body="yes",
        customer_phone=phone,
        created_at=_minutes_ago(minutes_ago),
    )


def _booking_requests(db_conn: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return db_conn.execute(
        "SELECT quote_id, customer_phone, notes FROM escalations "
        "WHERE reason = 'booking_requested' ORDER BY id"
    ).fetchall()


def _request(
    conn: psycopg.Connection[Any],
    conversation_id: int,
    *,
    validity: timedelta = _VALIDITY,
) -> dict[str, Any]:
    return request_booking_follow_up(
        conn, conversation_id=conversation_id, quote_validity=validity
    )


def test_a_yes_after_the_quote_opens_one_request_and_summarises_it(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = _conversation(db_conn)
    quote_id = _quote(db_conn, conversation_id, minutes_ago=19)
    _customer_writes(db_conn, conversation_id, minutes_ago=18)

    result = _request(db_conn, conversation_id)

    assert result.keys() == BOOKING_FOLLOW_UP_RESULT_KEYS
    assert result == {
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
    assert _booking_requests(db_conn) == [(quote_id, _PHONE, "{}")]


def test_saying_yes_twice_opens_only_one_request(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = _conversation(db_conn)
    _quote(db_conn, conversation_id, minutes_ago=19)
    _customer_writes(db_conn, conversation_id, minutes_ago=18)
    _request(db_conn, conversation_id)
    _customer_writes(db_conn, conversation_id, minutes_ago=17)

    again = _request(db_conn, conversation_id)

    assert again["already_requested"] is True
    assert len(_booking_requests(db_conn)) == 1


def test_the_latest_answered_quote_is_the_one_passed_on(
    db_conn: psycopg.Connection[Any],
) -> None:
    """A newer price the customer then says yes to is a new request; a price
    given after the customer's last message has not been answered yet."""
    conversation_id = _conversation(db_conn)
    first = _quote(db_conn, conversation_id, minutes_ago=19)
    second = _quote(
        db_conn, conversation_id, minutes_ago=15, check_in=date(2026, 10, 25)
    )
    _customer_writes(db_conn, conversation_id, minutes_ago=14)
    _quote(db_conn, conversation_id, minutes_ago=13, check_in=date(2026, 11, 1))

    result = _request(db_conn, conversation_id)

    assert result["quote_id"] == second
    assert result["check_in"] == "2026-10-25"
    assert [row[0] for row in _booking_requests(db_conn)] == [second]
    assert first != second


def test_each_answered_quote_gets_its_own_request(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = _conversation(db_conn)
    first = _quote(db_conn, conversation_id, minutes_ago=19)
    _customer_writes(db_conn, conversation_id, minutes_ago=18)
    _request(db_conn, conversation_id)
    second = _quote(db_conn, conversation_id, minutes_ago=10)
    _customer_writes(db_conn, conversation_id, minutes_ago=9)

    _request(db_conn, conversation_id)

    assert [row[0] for row in _booking_requests(db_conn)] == [first, second]


def test_no_message_since_the_quote_is_not_confirmable(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The tool answers a customer's message about a price they have seen,
    never the same turn that produced the price."""
    conversation_id = _conversation(db_conn)
    _quote(db_conn, conversation_id, minutes_ago=1)

    with pytest.raises(InvalidToolArgumentsError) as exc_info:
        _request(db_conn, conversation_id)

    assert exc_info.value.code == "quote_not_confirmable"
    assert _booking_requests(db_conn) == []


def test_an_expired_quote_is_not_confirmable(db_conn: psycopg.Connection[Any]) -> None:
    """Past the validity window the model must give a fresh price first."""
    conversation_id = _conversation(db_conn, opened=50)
    _quote(db_conn, conversation_id, minutes_ago=45)
    _customer_writes(db_conn, conversation_id, minutes_ago=1)

    with pytest.raises(InvalidToolArgumentsError) as exc_info:
        _request(db_conn, conversation_id)

    assert exc_info.value.code == "quote_not_confirmable"
    assert _booking_requests(db_conn) == []


def test_another_conversations_quote_is_never_picked(
    db_conn: psycopg.Connection[Any],
) -> None:
    other = _conversation(db_conn, phone=_OTHER_PHONE)
    _quote(db_conn, other, minutes_ago=19, phone=_OTHER_PHONE)
    conversation_id = _conversation(db_conn)
    _customer_writes(db_conn, conversation_id, minutes_ago=1)

    with pytest.raises(InvalidToolArgumentsError) as exc_info:
        _request(db_conn, conversation_id)

    assert exc_info.value.code == "quote_not_confirmable"


def test_a_quote_from_an_earlier_session_is_not_confirmable(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The same session scoping the output guard uses. The window is a day
    here, so only the session boundary (6 idle hours) can exclude it."""
    conversation_id = _conversation(db_conn, opened=8 * 60)
    _quote(db_conn, conversation_id, minutes_ago=8 * 60 - 1)
    _customer_writes(db_conn, conversation_id, minutes_ago=1)

    with pytest.raises(InvalidToolArgumentsError) as exc_info:
        _request(db_conn, conversation_id, validity=timedelta(days=1))

    assert exc_info.value.code == "quote_not_confirmable"


def test_runs_as_hotel_agent(
    db_conn: psycopg.Connection[Any], agent_database_url: str
) -> None:
    """Migration 0032's column grants, and 0030's hotels/room_types reads,
    are all the tool needs, as the role the webhook really connects as."""
    conversation_id = _conversation(db_conn)
    quote_id = _quote(db_conn, conversation_id, minutes_ago=19)
    _customer_writes(db_conn, conversation_id, minutes_ago=18)

    with psycopg.connect(agent_database_url, autocommit=True) as agent:
        first = _request(agent, conversation_id)
        second = _request(agent, conversation_id)

    assert (first["already_requested"], second["already_requested"]) == (False, True)
    assert first["hotel_name"] == "Test Hotel"
    assert _booking_requests(db_conn) == [(quote_id, _PHONE, "{}")]


def test_two_answers_at_the_same_moment_open_one_request(
    db_conn: psycopg.Connection[Any], agent_database_url: str
) -> None:
    """The race a check-then-insert would lose: the first answer's row is
    not committed yet when the second arrives. The unique index makes the
    second wait for the first, then do nothing."""
    conversation_id = _conversation(db_conn)
    quote_id = _quote(db_conn, conversation_id, minutes_ago=19)
    _customer_writes(db_conn, conversation_id, minutes_ago=18)
    second_result: list[dict[str, Any]] = []

    def _second_answer() -> None:
        with psycopg.connect(agent_database_url, autocommit=True) as second:
            second_result.append(_request(second, conversation_id))

    with psycopg.connect(agent_database_url) as first:
        first_result = _request(first, conversation_id)
        racer = threading.Thread(target=_second_answer)
        racer.start()
        racer.join(timeout=_BLOCK_OBSERVATION_SECONDS)
        assert racer.is_alive(), "the second answer did not wait for the first"
        first.commit()
    racer.join()

    assert first_result["already_requested"] is False
    (second,) = second_result
    assert (second["quote_id"], second["already_requested"]) == (quote_id, True)
    assert len(_booking_requests(db_conn)) == 1
