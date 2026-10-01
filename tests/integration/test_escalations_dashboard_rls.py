"""Migration 0033's read access for the dashboard's escalations screen,
against a real Postgres: every active admin and sales user reads every
escalation, and the conversation, messages and quotes of an escalated
conversation only -- never a conversation that did not escalate, never a
quote's nights or floor, and never any write (CLAUDE.md rules 2, 10)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from psycopg import sql

from tests.integration._seed import (
    seed_conversation,
    seed_escalation,
    seed_hotel_and_room_type,
    seed_message,
    seed_quote,
)

pytestmark = pytest.mark.usefixtures("db_conn")

_ESCALATED_PHONE = "+966500000001"
_QUIET_PHONE = "+966500000002"


def _seed_user(
    conn: psycopg.Connection[Any], *, role: str | None, is_active: bool = True
) -> str:
    """An auth.users identity, with an app_users row in `role` unless role
    is None (signed in, never provisioned)."""
    row = conn.execute("INSERT INTO auth.users DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    user_id = str(row[0])
    if role is not None:
        conn.execute(
            "INSERT INTO app_users (id, full_name, app_role, is_active) "
            "VALUES (%s, 'Test User', %s, %s)",
            (user_id, role, is_active),
        )
    return user_id


def _seed_conversation_with_quote(
    conn: psycopg.Connection[Any], phone: str, *, escalated: bool
) -> tuple[int, int]:
    conversation_id = seed_conversation(conn, customer_phone=phone)
    seed_message(
        conn, conversation_id, direction="inbound", body="price?", customer_phone=phone
    )
    seed_message(
        conn, conversation_id, direction="outbound", body="offer", customer_phone=phone
    )
    hotel_id, room_type_id = seed_hotel_and_room_type(conn)
    quote_id = seed_quote(
        conn,
        hotel_id,
        room_type_id,
        conversation_id=conversation_id,
        customer_phone=phone,
    )
    if escalated:
        seed_escalation(
            conn, conversation_id, reason="booking_requested", customer_phone=phone
        )
    return conversation_id, quote_id


@pytest.fixture
def two_conversations(db_conn: psycopg.Connection[Any]) -> tuple[int, int]:
    """One conversation that escalated and one that did not; returns their
    ids."""
    escalated, _ = _seed_conversation_with_quote(
        db_conn, _ESCALATED_PHONE, escalated=True
    )
    quiet, _ = _seed_conversation_with_quote(db_conn, _QUIET_PHONE, escalated=False)
    return escalated, quiet


@pytest.mark.parametrize("role", ["admin", "sales"])
def test_staff_read_every_escalation(
    db_conn: psycopg.Connection[Any],
    sign_in_as: Callable[[str], None],
    two_conversations: tuple[int, int],
    role: str,
) -> None:
    """Owner decision 2026-10-01: admin and sales both see all of them."""
    escalated, _ = two_conversations
    seed_escalation(db_conn, escalated, reason="delivery_failed")
    user_id = _seed_user(db_conn, role=role)

    sign_in_as(user_id)
    rows = db_conn.execute(
        "SELECT conversation_id, reason, notes, quote_id, assigned_to, "
        "responded_at, resolved_at FROM escalations ORDER BY id"
    ).fetchall()

    assert [(row[0], row[1]) for row in rows] == [
        (escalated, "booking_requested"),
        (escalated, "delivery_failed"),
    ]


@pytest.mark.parametrize(
    ("role", "is_active"),
    [
        pytest.param("sales", False, id="deactivated"),
        pytest.param(None, True, id="never-provisioned"),
    ],
)
@pytest.mark.parametrize(
    "query",
    [
        "SELECT id FROM escalations",
        "SELECT id FROM conversations",
        "SELECT id FROM messages",
        "SELECT id FROM quotes",
    ],
)
@pytest.mark.usefixtures("two_conversations")
def test_anyone_who_is_not_active_staff_reads_nothing(
    db_conn: psycopg.Connection[Any],
    sign_in_as: Callable[[str], None],
    role: str | None,
    is_active: bool,
    query: str,
) -> None:
    user_id = _seed_user(db_conn, role=role, is_active=is_active)

    sign_in_as(user_id)

    assert db_conn.execute(query).fetchall() == []


@pytest.mark.parametrize(
    "query",
    [
        "SELECT id FROM conversations",
        "SELECT DISTINCT conversation_id FROM messages",
        "SELECT DISTINCT conversation_id FROM quotes",
    ],
)
def test_staff_see_only_conversations_that_escalated(
    db_conn: psycopg.Connection[Any],
    sign_in_as: Callable[[str], None],
    two_conversations: tuple[int, int],
    query: str,
) -> None:
    escalated, _ = two_conversations
    user_id = _seed_user(db_conn, role="sales")

    sign_in_as(user_id)

    assert db_conn.execute(query).fetchall() == [(escalated,)]


def test_staff_read_the_columns_the_screen_shows(
    db_conn: psycopg.Connection[Any],
    sign_in_as: Callable[[str], None],
    two_conversations: tuple[int, int],
) -> None:
    escalated, _ = two_conversations
    user_id = _seed_user(db_conn, role="sales")

    sign_in_as(user_id)
    conversation = db_conn.execute(
        "SELECT id, customer_phone, last_message_at FROM conversations"
    ).fetchone()
    messages = db_conn.execute(
        "SELECT direction, body FROM messages ORDER BY created_at, id"
    ).fetchall()
    quote = db_conn.execute(
        "SELECT conversation_id, hotel_id, room_type_id, check_in, check_out, "
        "rooms, ask_price_total, created_at FROM quotes"
    ).fetchone()

    assert conversation is not None
    assert conversation[:2] == (escalated, _ESCALATED_PHONE)
    assert messages == [("inbound", "price?"), ("outbound", "offer")]
    assert quote is not None
    assert (quote[0], quote[6]) == (escalated, 20_000)


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("SELECT nights FROM quotes", id="quote-nights-cost-audit"),
        pytest.param("SELECT min_allowed_total FROM quotes", id="quote-floor"),
        pytest.param("SELECT * FROM quotes", id="quote-every-column"),
        pytest.param("SELECT turn_count FROM conversations", id="conversation-turns"),
        pytest.param(
            "SELECT whatsapp_message_id FROM messages", id="message-whatsapp-id"
        ),
        pytest.param("SELECT customer_phone FROM messages", id="message-phone"),
    ],
)
@pytest.mark.usefixtures("two_conversations")
def test_hidden_columns_are_refused(
    db_conn: psycopg.Connection[Any],
    sign_in_as: Callable[[str], None],
    query: str,
) -> None:
    """CLAUDE.md rule 2 and §8's cost masking: a quote's nights carry its
    cost audit trail and min_allowed_total is the floor."""
    user_id = _seed_user(db_conn, role="admin")

    sign_in_as(user_id)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute(query)


def test_a_quote_outside_any_conversation_is_invisible(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    seed_quote(db_conn, hotel_id, room_type_id)
    user_id = _seed_user(db_conn, role="admin")

    sign_in_as(user_id)

    assert db_conn.execute("SELECT id FROM quotes").fetchall() == []


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO escalations (conversation_id, customer_phone, reason) "
        "VALUES ({conversation_id}, '+966500000001', 'x')",
        "UPDATE escalations SET resolved_at = now()",
        "DELETE FROM escalations",
        "UPDATE conversations SET last_message_at = now()",
        "DELETE FROM conversations",
        "INSERT INTO messages (conversation_id, customer_phone, direction, body) "
        "VALUES ({conversation_id}, '+966500000001', 'outbound', 'x')",
        "UPDATE messages SET body = 'x'",
        "DELETE FROM messages",
        "UPDATE quotes SET rooms = 2",
        "DELETE FROM quotes",
    ],
)
def test_staff_can_write_none_of_these_tables(
    db_conn: psycopg.Connection[Any],
    sign_in_as: Callable[[str], None],
    two_conversations: tuple[int, int],
    statement: str,
) -> None:
    """Step 1 is read-only: taking over, resolving and replying are later
    steps with their own migrations."""
    escalated, _ = two_conversations
    user_id = _seed_user(db_conn, role="admin")

    sign_in_as(user_id)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute(
            sql.SQL(statement).format(conversation_id=sql.Literal(escalated))
        )


def test_escalations_and_messages_stream_through_supabase_realtime(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Owner decision 8: live updates. Only these two tables are published;
    conversations and quotes are re-read when an event arrives."""
    rows = db_conn.execute(
        "SELECT tablename FROM pg_publication_tables "
        "WHERE pubname = 'supabase_realtime' AND schemaname = 'public' "
        "ORDER BY tablename"
    ).fetchall()

    assert rows == [("escalations",), ("messages",)]
