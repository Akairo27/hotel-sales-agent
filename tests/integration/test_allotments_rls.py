"""Verifies migration 0016's write path for allotments.cost_per_night
against a real Postgres instance — see CLAUDE.md rule 3: the DB constraint
is the source of truth, not application discipline.

admin_set_allotment_cost is the only supported way to change cost_per_night;
the AFTER UPDATE trigger it relies on rejects any UPDATE that reaches the
row without app.actor_id set first, which is what makes bypassing the
wrapper function fail loudly instead of silently skipping the audit trail.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from tests.integration._seed import seed_actor

pytestmark = pytest.mark.usefixtures("db_conn")


def _seed_user(conn: psycopg.Connection[Any], *, role: str, can_view_cost: bool) -> str:
    row = conn.execute("INSERT INTO auth.users DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    user_id = str(row[0])
    conn.execute(
        "INSERT INTO app_users (id, full_name, app_role, can_view_cost) "
        "VALUES (%s, 'Test User', %s, %s)",
        (user_id, role, can_view_cost),
    )
    return user_id


def _seed_allotment(
    conn: psycopg.Connection[Any], *, cost_per_night: int = 15000
) -> int:
    # Migration 0029's audit trigger on allotments now fires on INSERT
    # too, not just UPDATE, and rejects a write with no app.actor_id set,
    # same as price_rules' and price_overrides'.
    seed_actor(conn)
    hotel_row = conn.execute(
        "INSERT INTO hotels (hotel_name) VALUES ('Test Hotel') RETURNING id"
    ).fetchone()
    assert hotel_row is not None
    room_type_row = conn.execute(
        "INSERT INTO room_types (hotel_id, room_type_name) VALUES (%s, 'Deluxe') "
        "RETURNING id",
        (hotel_row[0],),
    ).fetchone()
    assert room_type_row is not None
    allotment_row = conn.execute(
        "INSERT INTO allotments (hotel_id, room_type_id, stay_date, total_rooms, "
        "cost_per_night) VALUES (%s, %s, '2026-09-01', 5, %s) RETURNING id",
        (hotel_row[0], room_type_row[0], cost_per_night),
    ).fetchone()
    assert allotment_row is not None
    return int(allotment_row[0])


def test_admin_with_can_view_cost_can_update_via_wrapper(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    allotment_id = _seed_allotment(db_conn, cost_per_night=15000)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)

    db_conn.execute("SELECT admin_set_allotment_cost(%s, %s)", (allotment_id, 16000))

    row = db_conn.execute(
        "SELECT cost_per_night FROM allotments_for_dashboard WHERE id = %s",
        (allotment_id,),
    ).fetchone()
    assert row == (16000,)


def test_admin_with_can_view_cost_update_writes_audit_entry(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    allotment_id = _seed_allotment(db_conn, cost_per_night=15000)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)

    db_conn.execute("SELECT admin_set_allotment_cost(%s, %s)", (allotment_id, 16000))

    # ORDER BY id DESC LIMIT 1: seeding the allotment now writes its own
    # insert-audit rows too (migration 0029's trigger fires on INSERT), so
    # more than one row exists for this row_id — the one this UPDATE just
    # wrote is the last one.
    row = db_conn.execute(
        "SELECT column_name, old_value, new_value, changed_by::text FROM audit_log "
        "WHERE table_name = 'allotments' AND row_id = %s "
        "ORDER BY id DESC LIMIT 1",
        (str(allotment_id),),
    ).fetchone()
    assert row == ("cost_per_night", 15000, 16000, admin_id)


def test_admin_without_can_view_cost_cannot_update(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """An admin without cost visibility must not blindly overwrite a value
    they cannot see — the RLS policy gates on can_view_cost, not just role,
    so this fails as a real permission error, not a silent no-op."""
    allotment_id = _seed_allotment(db_conn, cost_per_night=15000)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=False)
    sign_in_as(admin_id)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute(
            "SELECT admin_set_allotment_cost(%s, %s)", (allotment_id, 16000)
        )

    db_conn.execute("RESET SESSION AUTHORIZATION")
    db_conn.execute("RESET request.jwt.claim.sub")
    row = db_conn.execute(
        "SELECT cost_per_night FROM allotments WHERE id = %s", (allotment_id,)
    ).fetchone()
    assert row == (15000,)


def test_sales_cannot_update_cost(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    allotment_id = _seed_allotment(db_conn, cost_per_night=15000)
    sales_id = _seed_user(db_conn, role="sales", can_view_cost=True)
    sign_in_as(sales_id)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute(
            "SELECT admin_set_allotment_cost(%s, %s)", (allotment_id, 16000)
        )


def test_bypassing_wrapper_is_rejected_not_silently_unlogged(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """The whole reason this is a trigger and not just admin_set_allotment_cost
    doing the INSERT itself: a write that reaches the row through any other
    path — including one that would otherwise satisfy RLS — must fail, not
    succeed unaudited. app.actor_id is never set on this direct path."""
    allotment_id = _seed_allotment(db_conn, cost_per_night=15000)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    # _seed_allotment now seeds an actor too (migration 0029's insert-audit
    # trigger needs one), which leaves app.actor_id set for the rest of
    # this session — reset it so the bypass attempt below genuinely starts
    # with no actor set, matching what this test is actually proving.
    db_conn.execute("RESET app.actor_id")
    sign_in_as(admin_id)

    with pytest.raises(psycopg.errors.RaiseException, match=r"app\.actor_id"):
        db_conn.execute(
            "UPDATE allotments SET cost_per_night = 99999 WHERE id = %s",
            (allotment_id,),
        )

    db_conn.execute("RESET SESSION AUTHORIZATION")
    db_conn.execute("RESET request.jwt.claim.sub")
    row = db_conn.execute(
        "SELECT cost_per_night FROM allotments WHERE id = %s", (allotment_id,)
    ).fetchone()
    assert row == (15000,)


def test_updating_an_allotment_without_changing_an_audited_value_needs_no_actor(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The original version of this test pinned "total_rooms is untracked,
    so writing it needs no actor" — migration 0029 (D6) deliberately makes
    that false: total_rooms is audited now, the same as cost_per_night.
    What still holds, and is worth pinning here, is the trigger's WHEN
    clause itself: a write that leaves every audited value exactly as it
    was (IS DISTINCT FROM false for both columns) must not fire the
    trigger, and so must not demand app.actor_id for what is, from the
    audit trail's point of view, a no-op."""
    allotment_id = _seed_allotment(db_conn, cost_per_night=15000)
    before = db_conn.execute(
        "SELECT count(*) FROM audit_log "
        "WHERE table_name = 'allotments' AND row_id = %s",
        (str(allotment_id),),
    ).fetchone()

    db_conn.execute("SET SESSION AUTHORIZATION service_role")
    try:
        db_conn.execute(
            "UPDATE allotments SET total_rooms = total_rooms WHERE id = %s",
            (allotment_id,),
        )
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")

    after = db_conn.execute(
        "SELECT count(*) FROM audit_log "
        "WHERE table_name = 'allotments' AND row_id = %s",
        (str(allotment_id),),
    ).fetchone()
    assert after == before
