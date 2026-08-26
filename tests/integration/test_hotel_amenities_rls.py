"""Verifies migration 0023's grants and RLS policies on hotel_amenities
against a real Postgres instance — see CLAUDE.md rule 3: the DB constraint
is the source of truth, not application discipline.

Same split migration 0014 applies to hotels itself: read for any active
app_users row, write for admins only. Amenities are added and removed, never
edited in place, so UPDATE is granted to no role but service_role.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
import pytest

pytestmark = pytest.mark.usefixtures("db_conn")


def _seed_user(conn: psycopg.Connection[Any], *, role: str = "sales") -> str:
    row = conn.execute("INSERT INTO auth.users DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    user_id = str(row[0])
    conn.execute(
        "INSERT INTO app_users (id, full_name, app_role) VALUES (%s, 'Test User', %s)",
        (user_id, role),
    )
    return user_id


def _seed_hotel_with_amenity(
    conn: psycopg.Connection[Any], amenity: str = "haram_view"
) -> int:
    row = conn.execute(
        "INSERT INTO hotels (hotel_name) VALUES ('Test Hotel') RETURNING id"
    ).fetchone()
    assert row is not None
    hotel_id = int(row[0])
    conn.execute(
        "INSERT INTO hotel_amenities (hotel_id, amenity) VALUES (%s, %s)",
        (hotel_id, amenity),
    )
    return hotel_id


def test_sales_can_select_amenities(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id = _seed_hotel_with_amenity(db_conn)
    sign_in_as(_seed_user(db_conn, role="sales"))

    rows = db_conn.execute("SELECT hotel_id, amenity FROM hotel_amenities").fetchall()
    assert rows == [(hotel_id, "haram_view")]


def test_sales_cannot_insert_amenity(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id = _seed_hotel_with_amenity(db_conn)
    sign_in_as(_seed_user(db_conn, role="sales"))

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute(
            "INSERT INTO hotel_amenities (hotel_id, amenity) VALUES (%s, 'wifi')",
            (hotel_id,),
        )


def test_sales_cannot_delete_amenity(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """A denied DELETE does not raise, for the same reason a denied UPDATE
    doesn't (see test_hotels_room_types_rls): the row simply isn't in the
    set the USING clause makes visible, so the statement matches zero rows
    silently. Rowcount is the real assertion, not an exception."""
    hotel_id = _seed_hotel_with_amenity(db_conn)
    sign_in_as(_seed_user(db_conn, role="sales"))

    cursor = db_conn.execute(
        "DELETE FROM hotel_amenities WHERE hotel_id = %s", (hotel_id,)
    )
    assert cursor.rowcount == 0

    remaining = db_conn.execute("SELECT count(*) FROM hotel_amenities").fetchone()
    assert remaining is not None
    assert remaining[0] == 1


def test_admin_can_insert_amenity(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id = _seed_hotel_with_amenity(db_conn)
    sign_in_as(_seed_user(db_conn, role="admin"))

    db_conn.execute(
        "INSERT INTO hotel_amenities (hotel_id, amenity) VALUES (%s, 'wifi')",
        (hotel_id,),
    )

    rows = db_conn.execute(
        "SELECT amenity FROM hotel_amenities ORDER BY amenity"
    ).fetchall()
    assert [r[0] for r in rows] == ["haram_view", "wifi"]


def test_admin_can_delete_amenity(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id = _seed_hotel_with_amenity(db_conn)
    sign_in_as(_seed_user(db_conn, role="admin"))

    cursor = db_conn.execute(
        "DELETE FROM hotel_amenities WHERE hotel_id = %s AND amenity = 'haram_view'",
        (hotel_id,),
    )
    assert cursor.rowcount == 1


def test_admin_cannot_update_amenity(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """UPDATE is granted to no role but service_role: an amenity row is its
    own key, so changing one is a delete plus an insert. This fails at the
    grant level, before any policy is consulted."""
    hotel_id = _seed_hotel_with_amenity(db_conn)
    sign_in_as(_seed_user(db_conn, role="admin"))

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute(
            "UPDATE hotel_amenities SET amenity = 'wifi' WHERE hotel_id = %s",
            (hotel_id,),
        )


def test_inactive_admin_cannot_insert_amenity(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """current_app_role() returns NULL for a deactivated row, so an
    ex-admin's writes fail the WITH CHECK even though the grant stands."""
    hotel_id = _seed_hotel_with_amenity(db_conn)
    admin_id = _seed_user(db_conn, role="admin")
    db_conn.execute("UPDATE app_users SET is_active = false WHERE id = %s", (admin_id,))
    sign_in_as(admin_id)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute(
            "INSERT INTO hotel_amenities (hotel_id, amenity) VALUES (%s, 'wifi')",
            (hotel_id,),
        )


def test_rls_denies_anon_on_hotel_amenities(db_conn: psycopg.Connection[Any]) -> None:
    """anon has no grant on hotel_amenities at all — same as every other
    table in this schema — so this fails before RLS is even reached: without
    schema USAGE, Postgres cannot resolve the unqualified table name for
    that role and raises UndefinedTable, not a table-level permission
    error. Same expectation as test_rls_denies_anon_on_hotels."""
    _seed_hotel_with_amenity(db_conn)
    db_conn.execute("SET SESSION AUTHORIZATION anon")
    try:
        with pytest.raises(psycopg.errors.UndefinedTable):
            db_conn.execute("SELECT * FROM hotel_amenities").fetchall()
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")
