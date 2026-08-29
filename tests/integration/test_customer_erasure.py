"""Verifies migration 0013's one narrow exception to quotes' append-only
guarantee: quotes_erase_customer_phone(), a SECURITY DEFINER function that
can only ever set customer_phone to NULL for rows matching a given phone
number — never any other column, never a row deletion. Also verifies the
two erasure mechanisms migration 0024 adds for the same right, one per
ARCHITECTURE.md §10 table: conversations_erase_customer (real DELETE,
cascading to messages/escalations) and bookings_erase_customer (NULL-out,
same shape as quotes').

service_role's direct UPDATE/DELETE rejection on quotes and audit_log is
already covered by test_schema_constraints.py's
test_quotes_is_append_only_*_rejected and test_audit_log_rls.py's
test_audit_log_is_append_only_*_rejected — not duplicated here.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from tests.integration._seed import (
    seed_conversation,
    seed_hold,
    seed_hotel_and_room_type,
    seed_quote,
)

pytestmark = pytest.mark.usefixtures("db_conn")


def _seed_quote(db_conn: psycopg.Connection[Any], *, customer_phone: str) -> int:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    return seed_quote(db_conn, hotel_id, room_type_id, customer_phone=customer_phone)


def test_erase_customer_phone_zeroes_only_the_matching_phone(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The pricing record itself — ask/min_allowed/nights, the numbers
    CLAUDE.md rule 3 cares about — must survive untouched. A second
    customer's quote must survive untouched too: the function targets by
    exact phone match, not a blanket wipe."""
    target_id = _seed_quote(db_conn, customer_phone="+966500000001")
    other_id = _seed_quote(db_conn, customer_phone="+966500000002")

    db_conn.execute("SET SESSION AUTHORIZATION service_role")
    try:
        erased = db_conn.execute(
            "SELECT quotes_erase_customer_phone(%s)", ("+966500000001",)
        ).fetchone()
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")

    assert erased == (1,)
    rows = db_conn.execute(
        "SELECT id, customer_phone, ask_price_total, min_allowed_total, nights "
        "FROM quotes WHERE id IN (%s, %s) ORDER BY id",
        (target_id, other_id),
    ).fetchall()
    by_id = {row[0]: row[1:] for row in rows}
    assert by_id[target_id][0] is None
    assert by_id[target_id][1:] == (
        20000,
        10000,
        [
            {
                "date": "2026-09-01",
                "season_id": 1,
                "ask": 20000,
                "min_allowed": 10000,
                "override_applied": True,
            }
        ],
    )
    assert by_id[other_id][0] == "+966500000002"


def test_erase_customer_phone_matching_nothing_is_a_safe_no_op(
    db_conn: psycopg.Connection[Any],
) -> None:
    quote_id = _seed_quote(db_conn, customer_phone="+966500000001")

    db_conn.execute("SET SESSION AUTHORIZATION service_role")
    try:
        erased = db_conn.execute(
            "SELECT quotes_erase_customer_phone(%s)", ("+966599999999",)
        ).fetchone()
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")

    assert erased == (0,)
    phone = db_conn.execute(
        "SELECT customer_phone FROM quotes WHERE id = %s", (quote_id,)
    ).fetchone()
    assert phone == ("+966500000001",)


def test_authenticated_cannot_call_erase_customer_phone(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Trip-wire, same spirit as test_cost_tables_rls.py: migration
    0012's global function-default lockdown already denies this by
    default — this fails loudly the day a future migration grants
    EXECUTE on this specific function too broadly. authenticated has
    schema USAGE (migration 0010), so this is a real permission denial,
    not a missing-schema error."""
    db_conn.execute("SET SESSION AUTHORIZATION authenticated")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute(
                "SELECT quotes_erase_customer_phone(%s)", ("+966500000001",)
            )
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


def test_anon_cannot_call_erase_customer_phone(
    db_conn: psycopg.Connection[Any],
) -> None:
    """anon has no schema USAGE at all locally, so this fails one step
    earlier than the authenticated case — same local-vs-real-Supabase
    divergence documented in test_default_privileges_lockdown.py; the
    security outcome (zero access), not the error code, is what matters."""
    db_conn.execute("SET SESSION AUTHORIZATION anon")
    try:
        with pytest.raises(psycopg.errors.UndefinedFunction):
            db_conn.execute(
                "SELECT quotes_erase_customer_phone(%s)", ("+966500000001",)
            )
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


def test_erase_conversation_customer_deletes_conversation_and_cascades(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Unlike quotes, conversations/messages/escalations have no audited
    financial record to preserve — erasure is a real DELETE, and
    ON DELETE CASCADE (migration 0024) takes messages/escalations with it.
    A second customer's conversation must survive untouched.

    Also seeds a quote pointing at the target conversation — the normal
    case for any real sales conversation that reached pricing, and the one
    that broke this function outright before quotes_conversation_id_fkey
    was given ON DELETE SET NULL: a plain FK RESTRICT here would have
    raised ForeignKeyViolation and left the conversation (and the customer's
    data) un-erased."""
    target_id = seed_conversation(db_conn, customer_phone="+966500000001")
    other_id = seed_conversation(db_conn, customer_phone="+966500000002")
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    quote_id = seed_quote(
        db_conn,
        hotel_id,
        room_type_id,
        customer_phone="+966500000001",
        conversation_id=target_id,
    )
    db_conn.execute(
        "INSERT INTO messages (conversation_id, customer_phone, direction, body) "
        "VALUES (%s, '+966500000001', 'inbound', 'hello')",
        (target_id,),
    )
    db_conn.execute(
        "INSERT INTO escalations (conversation_id, customer_phone, reason) "
        "VALUES (%s, '+966500000001', 'customer requested a human')",
        (target_id,),
    )

    db_conn.execute("SET SESSION AUTHORIZATION service_role")
    try:
        erased = db_conn.execute(
            "SELECT conversations_erase_customer(%s)", ("+966500000001",)
        ).fetchone()
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")

    assert erased == (1,)
    remaining_conversations = db_conn.execute(
        "SELECT id FROM conversations ORDER BY id"
    ).fetchall()
    assert remaining_conversations == [(other_id,)]
    assert db_conn.execute("SELECT count(*) FROM messages").fetchone() == (0,)
    assert db_conn.execute("SELECT count(*) FROM escalations").fetchone() == (0,)

    # The quote itself must survive, unlinked — append-only per CLAUDE.md
    # rule 3, regardless of what happens to the conversation that produced
    # it. customer_phone stays as-is here: quotes_erase_customer_phone is
    # the separate, already-existing mechanism for scrubbing that.
    quote_row = db_conn.execute(
        "SELECT conversation_id, customer_phone, ask_price_total "
        "FROM quotes WHERE id = %s",
        (quote_id,),
    ).fetchone()
    assert quote_row == (None, "+966500000001", 20000)


def test_erase_conversation_customer_matching_nothing_is_a_safe_no_op(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id = seed_conversation(db_conn, customer_phone="+966500000001")

    db_conn.execute("SET SESSION AUTHORIZATION service_role")
    try:
        erased = db_conn.execute(
            "SELECT conversations_erase_customer(%s)", ("+966599999999",)
        ).fetchone()
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")

    assert erased == (0,)
    remaining = db_conn.execute(
        "SELECT id FROM conversations WHERE id = %s", (conversation_id,)
    ).fetchone()
    assert remaining == (conversation_id,)


def test_erase_booking_customer_nulls_only_personal_columns(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The accounting record — status/payment_status/accounting_sync_status/
    hold_id/quote_id — must survive untouched, same reasoning as quotes. A
    second customer's booking must survive untouched too."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    target_quote = seed_quote(
        db_conn, hotel_id, room_type_id, customer_phone="+966500000001"
    )
    target_hold = seed_hold(db_conn, hotel_id, room_type_id)
    other_quote = seed_quote(
        db_conn, hotel_id, room_type_id, customer_phone="+966500000002"
    )
    other_hold = seed_hold(db_conn, hotel_id, room_type_id)
    target_id = _seed_booking(
        db_conn,
        hold_id=target_hold,
        quote_id=target_quote,
        customer_phone="+966500000001",
    )
    other_id = _seed_booking(
        db_conn,
        hold_id=other_hold,
        quote_id=other_quote,
        customer_phone="+966500000002",
    )

    db_conn.execute("SET SESSION AUTHORIZATION service_role")
    try:
        erased = db_conn.execute(
            "SELECT bookings_erase_customer(%s)", ("+966500000001",)
        ).fetchone()
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")

    assert erased == (1,)
    rows = db_conn.execute(
        "SELECT id, customer_phone, full_name, nationality, status, "
        "payment_status, accounting_sync_status, hold_id, quote_id "
        "FROM bookings WHERE id IN (%s, %s) ORDER BY id",
        (target_id, other_id),
    ).fetchall()
    by_id = {row[0]: row[1:] for row in rows}
    assert by_id[target_id] == (
        None,
        None,
        None,
        "confirmed",
        "pending",
        "pending",
        target_hold,
        target_quote,
    )
    assert by_id[other_id][:3] == ("+966500000002", "Other Guest", "Saudi")


def _seed_booking(
    db_conn: psycopg.Connection[Any],
    *,
    hold_id: int,
    quote_id: int,
    customer_phone: str,
) -> int:
    row = db_conn.execute(
        "INSERT INTO bookings (hold_id, quote_id, customer_phone, full_name, "
        "nationality, payment_status) "
        "VALUES (%s, %s, %s, 'Other Guest', 'Saudi', 'pending') RETURNING id",
        (hold_id, quote_id, customer_phone),
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_authenticated_cannot_call_erase_conversation_customer(
    db_conn: psycopg.Connection[Any],
) -> None:
    db_conn.execute("SET SESSION AUTHORIZATION authenticated")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute(
                "SELECT conversations_erase_customer(%s)", ("+966500000001",)
            )
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


def test_authenticated_cannot_call_erase_booking_customer(
    db_conn: psycopg.Connection[Any],
) -> None:
    db_conn.execute("SET SESSION AUTHORIZATION authenticated")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute("SELECT bookings_erase_customer(%s)", ("+966500000001",))
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")
