"""Trip-wire: the admin dashboard's `authenticated` role must never be able
to SELECT a cost-bearing table directly, until whichever PR grants it
access pairs that grant with column-level cost masking (see
ARCHITECTURE.md §8, "إخفاء عمود التكلفة"). This test carried no value in
proving protection before migration 0016 — that was already covered by
CLAUDE.md rule 3 (the REVOKE/GRANT is the source of truth) and the
migrations' own comments. Its value was failing loudly the day something
granted `authenticated` `SELECT` on one of these tables without also
shipping masking — migration 0016 (PR D) is that day, for `allotments`
only. `quotes` and `room_night_inventory` are untouched by this PR (no
screen reads either yet — a future PR's job, per the phase-3 decision) and
keep the original blanket-denial assertion unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from psycopg import sql

pytestmark = pytest.mark.usefixtures("db_conn")

_STILL_FULLY_LOCKED_TABLES = ("quotes", "room_night_inventory")


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


@pytest.mark.parametrize("table", _STILL_FULLY_LOCKED_TABLES)
def test_authenticated_cannot_select_cost_table(
    db_conn: psycopg.Connection[Any], table: str
) -> None:
    """authenticated has schema USAGE (migration 0010) but no table-level
    grant on either of these — permission denied at the table, not a
    missing schema. If this ever starts returning rows instead, cost data
    is reachable from the browser without column masking in place."""
    select = sql.SQL("SELECT * FROM {table}").format(table=sql.Identifier(table))
    db_conn.execute("SET SESSION AUTHORIZATION authenticated")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute(select)
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


@pytest.mark.parametrize("table", _STILL_FULLY_LOCKED_TABLES)
def test_anon_cannot_select_cost_table(
    db_conn: psycopg.Connection[Any], table: str
) -> None:
    """anon has schema USAGE (tests/supabase_default_acl_baseline.sql
    simulates the real Supabase default) but no table-level grant on any
    of these — permission denied at the table, same failure mode as
    authenticated; see test_rls_denies_anon in
    test_app_users_and_roles_rls.py for why."""
    select = sql.SQL("SELECT * FROM {table}").format(table=sql.Identifier(table))
    db_conn.execute("SET SESSION AUTHORIZATION anon")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute(select)
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


def test_authenticated_cannot_select_allotments_base_table(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """The masking below only holds if the base table stays unreachable —
    authenticated has a column-scoped SELECT (id only, migration 0016's
    write-verification need), never cost_per_night. Querying `id` alone
    still must not surface `cost_per_night` in the same row."""
    allotment_id = _seed_allotment(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute(
            "SELECT cost_per_night FROM allotments WHERE id = %s", (allotment_id,)
        )


def test_admin_with_can_view_cost_sees_real_cost_via_view(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    allotment_id = _seed_allotment(db_conn, cost_per_night=15000)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)

    row = db_conn.execute(
        "SELECT cost_per_night FROM allotments_for_dashboard WHERE id = %s",
        (allotment_id,),
    ).fetchone()
    assert row == (15000,)


def test_sales_without_can_view_cost_sees_null_via_view(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    allotment_id = _seed_allotment(db_conn, cost_per_night=15000)
    sales_id = _seed_user(db_conn, role="sales", can_view_cost=False)
    sign_in_as(sales_id)

    row = db_conn.execute(
        "SELECT cost_per_night FROM allotments_for_dashboard WHERE id = %s",
        (allotment_id,),
    ).fetchone()
    assert row == (None,)


def test_admin_without_can_view_cost_sees_null_via_view(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """can_view_cost is independent of role (migration 0010's own design) —
    an admin flipped off sees NULL exactly like a sales user does."""
    allotment_id = _seed_allotment(db_conn, cost_per_night=15000)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=False)
    sign_in_as(admin_id)

    row = db_conn.execute(
        "SELECT cost_per_night FROM allotments_for_dashboard WHERE id = %s",
        (allotment_id,),
    ).fetchone()
    assert row == (None,)


def test_view_masks_cost_without_hiding_other_columns(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """The point of a VIEW over a column-level GRANT (ARCHITECTURE.md §8):
    one query serves both roles — non-cost columns stay visible to sales
    the same as to admin, only cost_per_night differs."""
    allotment_id = _seed_allotment(db_conn, cost_per_night=15000)
    sales_id = _seed_user(db_conn, role="sales", can_view_cost=False)
    sign_in_as(sales_id)

    row = db_conn.execute(
        "SELECT id, total_rooms, cost_per_night FROM allotments_for_dashboard "
        "WHERE id = %s",
        (allotment_id,),
    ).fetchone()
    assert row == (allotment_id, 5, None)
