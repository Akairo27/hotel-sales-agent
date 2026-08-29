"""Verifies migration 0024's constraints, cascade behaviour, and grants for
conversations/messages/escalations/bookings against a real Postgres
instance — see CLAUDE.md rule 3: the DB constraint is the source of truth,
not application discipline.

Right-to-erasure behaviour (conversations_erase_customer,
bookings_erase_customer) is covered in test_customer_erasure.py alongside
quotes_erase_customer_phone, not duplicated here.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg import sql

from tests.integration._seed import (
    seed_conversation,
    seed_hold,
    seed_hotel_and_room_type,
    seed_quote,
)

pytestmark = pytest.mark.usefixtures("db_conn")


def _seed_booking(
    db_conn: psycopg.Connection[Any], *, hold_id: int, quote_id: int
) -> int:
    row = db_conn.execute(
        "INSERT INTO bookings (hold_id, quote_id, customer_phone, payment_status) "
        "VALUES (%s, %s, '+966500000001', 'pending') RETURNING id",
        (hold_id, quote_id),
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_conversations_concession_count_cannot_be_negative(
    db_conn: psycopg.Connection[Any],
) -> None:
    with pytest.raises(
        psycopg.errors.CheckViolation,
        match="conversations_concession_count_non_negative",
    ):
        db_conn.execute(
            "INSERT INTO conversations (customer_phone, concession_count) "
            "VALUES ('+966500000001', -1)"
        )


def test_conversations_turn_count_cannot_be_negative(
    db_conn: psycopg.Connection[Any],
) -> None:
    with pytest.raises(
        psycopg.errors.CheckViolation, match="conversations_turn_count_non_negative"
    ):
        db_conn.execute(
            "INSERT INTO conversations (customer_phone, turn_count) "
            "VALUES ('+966500000001', -1)"
        )


def test_messages_direction_must_be_valid(db_conn: psycopg.Connection[Any]) -> None:
    conversation_id = seed_conversation(db_conn)

    with pytest.raises(psycopg.errors.CheckViolation, match="messages_direction_valid"):
        db_conn.execute(
            "INSERT INTO messages (conversation_id, customer_phone, direction, body) "
            "VALUES (%s, '+966500000001', 'sideways', 'hi')",
            (conversation_id,),
        )


def test_messages_whatsapp_message_id_must_be_unique_when_present(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn)
    db_conn.execute(
        "INSERT INTO messages (conversation_id, customer_phone, direction, "
        "whatsapp_message_id, body) "
        "VALUES (%s, '+966500000001', 'inbound', 'wamid.dup', 'hi')",
        (conversation_id,),
    )

    with pytest.raises(
        psycopg.errors.UniqueViolation, match="messages_whatsapp_message_id_unique"
    ):
        db_conn.execute(
            "INSERT INTO messages (conversation_id, customer_phone, direction, "
            "whatsapp_message_id, body) "
            "VALUES (%s, '+966500000001', 'inbound', 'wamid.dup', 'hi again')",
            (conversation_id,),
        )


def test_messages_multiple_outbound_with_no_whatsapp_id_do_not_conflict(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The unique index is partial (WHERE whatsapp_message_id IS NOT NULL):
    outbound messages the agent generates have no WhatsApp id at all, and
    NULL never conflicts with NULL."""
    conversation_id = seed_conversation(db_conn)
    for _ in range(2):
        db_conn.execute(
            "INSERT INTO messages (conversation_id, customer_phone, direction, body) "
            "VALUES (%s, '+966500000001', 'outbound', 'hi')",
            (conversation_id,),
        )

    count = db_conn.execute("SELECT count(*) FROM messages").fetchone()
    assert count == (2,)


def test_escalations_reason_cannot_be_blank(db_conn: psycopg.Connection[Any]) -> None:
    conversation_id = seed_conversation(db_conn)

    with pytest.raises(
        psycopg.errors.CheckViolation, match="escalations_reason_not_blank"
    ):
        db_conn.execute(
            "INSERT INTO escalations (conversation_id, customer_phone, reason) "
            "VALUES (%s, '+966500000001', '   ')",
            (conversation_id,),
        )


def test_escalations_responded_at_cannot_precede_opened_at(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn)

    with pytest.raises(
        psycopg.errors.CheckViolation, match="escalations_responded_after_opened"
    ):
        db_conn.execute(
            "INSERT INTO escalations (conversation_id, customer_phone, reason, "
            "opened_at, responded_at) "
            "VALUES (%s, '+966500000001', 'cash on arrival', now(), "
            "now() - interval '1 hour')",
            (conversation_id,),
        )


def test_escalations_resolved_at_cannot_precede_opened_at(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn)

    with pytest.raises(
        psycopg.errors.CheckViolation, match="escalations_resolved_after_opened"
    ):
        db_conn.execute(
            "INSERT INTO escalations (conversation_id, customer_phone, reason, "
            "opened_at, resolved_at) "
            "VALUES (%s, '+966500000001', 'cash on arrival', now(), "
            "now() - interval '1 hour')",
            (conversation_id,),
        )


def test_messages_and_escalations_are_deleted_when_conversation_is_deleted(
    db_conn: psycopg.Connection[Any],
) -> None:
    """ON DELETE CASCADE (migration 0024) — the mechanism the
    conversations_erase_customer function relies on, exercised here via a
    plain DELETE rather than that function, so this test stands on its own
    even if the function's own logic ever changes."""
    conversation_id = seed_conversation(db_conn)
    db_conn.execute(
        "INSERT INTO messages (conversation_id, customer_phone, direction, body) "
        "VALUES (%s, '+966500000001', 'inbound', 'hi')",
        (conversation_id,),
    )
    db_conn.execute(
        "INSERT INTO escalations (conversation_id, customer_phone, reason) "
        "VALUES (%s, '+966500000001', 'cash on arrival')",
        (conversation_id,),
    )

    db_conn.execute("DELETE FROM conversations WHERE id = %s", (conversation_id,))

    assert db_conn.execute("SELECT count(*) FROM messages").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM escalations").fetchone() == (0,)


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("status", "checked_in", "bookings_status_valid"),
        ("payment_status", "half_paid", "bookings_payment_status_valid"),
        ("accounting_sync_status", "unknown", "bookings_accounting_sync_status_valid"),
    ],
)
def test_bookings_enum_columns_reject_invalid_values(
    db_conn: psycopg.Connection[Any], column: str, value: str, constraint: str
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    quote_id = seed_quote(db_conn, hotel_id, room_type_id)
    hold_id = seed_hold(db_conn, hotel_id, room_type_id)
    booking_id = _seed_booking(db_conn, hold_id=hold_id, quote_id=quote_id)

    update = sql.SQL("UPDATE bookings SET {column} = %s WHERE id = %s").format(
        column=sql.Identifier(column)
    )
    with pytest.raises(psycopg.errors.CheckViolation, match=constraint):
        db_conn.execute(update, (value, booking_id))


def test_bookings_hold_id_must_be_unique(db_conn: psycopg.Connection[Any]) -> None:
    """One booking per hold — a hold is confirmed into exactly one
    booking, never split or reused (ARCHITECTURE.md §6)."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    hold_id = seed_hold(db_conn, hotel_id, room_type_id)
    quote_a = seed_quote(db_conn, hotel_id, room_type_id)
    quote_b = seed_quote(db_conn, hotel_id, room_type_id)
    _seed_booking(db_conn, hold_id=hold_id, quote_id=quote_a)

    with pytest.raises(psycopg.errors.UniqueViolation):
        _seed_booking(db_conn, hold_id=hold_id, quote_id=quote_b)


def test_service_role_cannot_update_booking_customer_phone_directly(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Migration 0024 grants service_role column-level UPDATE on only
    status/payment_status/accounting_sync_status — customer_phone (and
    full_name/nationality) are reachable only through
    bookings_erase_customer. A bare UPDATE must fail at the grant level,
    same class of protection as quotes' append-only lockdown (migration
    0013), before any RLS policy is even consulted."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    quote_id = seed_quote(db_conn, hotel_id, room_type_id)
    hold_id = seed_hold(db_conn, hotel_id, room_type_id)
    booking_id = _seed_booking(db_conn, hold_id=hold_id, quote_id=quote_id)

    db_conn.execute("SET SESSION AUTHORIZATION service_role")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute(
                "UPDATE bookings SET customer_phone = '+966500000099' WHERE id = %s",
                (booking_id,),
            )
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


def test_service_role_can_update_booking_status(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The column-level restriction above must not also block the columns
    it is meant to leave open — a cancelled booking is a normal, expected
    write."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    quote_id = seed_quote(db_conn, hotel_id, room_type_id)
    hold_id = seed_hold(db_conn, hotel_id, room_type_id)
    booking_id = _seed_booking(db_conn, hold_id=hold_id, quote_id=quote_id)

    db_conn.execute("SET SESSION AUTHORIZATION service_role")
    try:
        db_conn.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE id = %s", (booking_id,)
        )
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")

    status = db_conn.execute(
        "SELECT status FROM bookings WHERE id = %s", (booking_id,)
    ).fetchone()
    assert status == ("cancelled",)


@pytest.mark.parametrize(
    "table_name", ["conversations", "messages", "escalations", "bookings"]
)
def test_rls_denies_anon_on_agent_tables(
    db_conn: psycopg.Connection[Any], table_name: str
) -> None:
    """anon has no grant on any of the four tables — same as every other
    table in this schema — so this fails before RLS is even reached:
    without schema USAGE, Postgres cannot resolve the unqualified table
    name for that role. Same expectation as
    test_rls_denies_anon_on_hotel_amenities."""
    select = sql.SQL("SELECT * FROM {table}").format(table=sql.Identifier(table_name))
    db_conn.execute("SET SESSION AUTHORIZATION anon")
    try:
        with pytest.raises(psycopg.errors.UndefinedTable):
            db_conn.execute(select)
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")
