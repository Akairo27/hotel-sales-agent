"""Verifies migration 0014's grants and RLS policies on hotels/room_types
against a real Postgres instance — see CLAUDE.md rule 3: the DB constraint
is the source of truth, not application discipline.

Read access is open to any active app_users row; writes (INSERT/UPDATE)
are admin-only; DELETE is granted to no role but service_role.
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


def _seed_hotel(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute(
        "INSERT INTO hotels (hotel_name) VALUES ('Test Hotel') RETURNING id"
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_sales_can_select_hotels(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id = _seed_hotel(db_conn)
    sales_id = _seed_user(db_conn, role="sales")
    sign_in_as(sales_id)

    rows = db_conn.execute("SELECT id FROM hotels").fetchall()
    assert [r[0] for r in rows] == [hotel_id]


def test_sales_can_select_room_types(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id = _seed_hotel(db_conn)
    room_type_row = db_conn.execute(
        "INSERT INTO room_types (hotel_id, room_type_name) VALUES (%s, 'Deluxe') "
        "RETURNING id",
        (hotel_id,),
    ).fetchone()
    assert room_type_row is not None
    sales_id = _seed_user(db_conn, role="sales")
    sign_in_as(sales_id)

    rows = db_conn.execute("SELECT id FROM room_types").fetchall()
    assert [r[0] for r in rows] == [room_type_row[0]]


def test_sales_cannot_insert_hotel(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    sales_id = _seed_user(db_conn, role="sales")
    sign_in_as(sales_id)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute("INSERT INTO hotels (hotel_name) VALUES ('New Hotel')")


def test_sales_cannot_update_hotel(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """Unlike INSERT's WITH CHECK, a denied UPDATE does not raise — the
    row simply isn't in the set current_app_role() = 'admin' USING makes
    visible for the UPDATE, so it matches zero rows silently. Asserting on
    rowcount/unchanged data is the real assertion here, not an exception."""
    hotel_id = _seed_hotel(db_conn)
    sales_id = _seed_user(db_conn, role="sales")
    sign_in_as(sales_id)

    cursor = db_conn.execute(
        "UPDATE hotels SET hotel_name = 'Renamed' WHERE id = %s", (hotel_id,)
    )
    assert cursor.rowcount == 0

    row = db_conn.execute(
        "SELECT hotel_name FROM hotels WHERE id = %s", (hotel_id,)
    ).fetchone()
    assert row == ("Test Hotel",)


def test_sales_cannot_update_room_type(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id = _seed_hotel(db_conn)
    room_type_row = db_conn.execute(
        "INSERT INTO room_types (hotel_id, room_type_name) VALUES (%s, 'Deluxe') "
        "RETURNING id",
        (hotel_id,),
    ).fetchone()
    assert room_type_row is not None
    sales_id = _seed_user(db_conn, role="sales")
    sign_in_as(sales_id)

    cursor = db_conn.execute(
        "UPDATE room_types SET room_type_name = 'Renamed' WHERE id = %s",
        (room_type_row[0],),
    )
    assert cursor.rowcount == 0


def test_sales_cannot_insert_room_type(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id = _seed_hotel(db_conn)
    sales_id = _seed_user(db_conn, role="sales")
    sign_in_as(sales_id)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute(
            "INSERT INTO room_types (hotel_id, room_type_name) VALUES (%s, 'Suite')",
            (hotel_id,),
        )


def test_admin_can_insert_hotel(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    admin_id = _seed_user(db_conn, role="admin")
    sign_in_as(admin_id)

    row = db_conn.execute(
        "INSERT INTO hotels (hotel_name) VALUES ('New Hotel') RETURNING id"
    ).fetchone()
    assert row is not None


def test_admin_can_update_hotel(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id = _seed_hotel(db_conn)
    admin_id = _seed_user(db_conn, role="admin")
    sign_in_as(admin_id)

    db_conn.execute(
        "UPDATE hotels SET hotel_name = 'Renamed' WHERE id = %s", (hotel_id,)
    )

    row = db_conn.execute(
        "SELECT hotel_name FROM hotels WHERE id = %s", (hotel_id,)
    ).fetchone()
    assert row == ("Renamed",)


def test_admin_can_insert_room_type(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id = _seed_hotel(db_conn)
    admin_id = _seed_user(db_conn, role="admin")
    sign_in_as(admin_id)

    row = db_conn.execute(
        "INSERT INTO room_types (hotel_id, room_type_name) VALUES (%s, 'Suite') "
        "RETURNING id",
        (hotel_id,),
    ).fetchone()
    assert row is not None


def test_admin_can_update_room_type(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id = _seed_hotel(db_conn)
    room_type_row = db_conn.execute(
        "INSERT INTO room_types (hotel_id, room_type_name) VALUES (%s, 'Deluxe') "
        "RETURNING id",
        (hotel_id,),
    ).fetchone()
    assert room_type_row is not None
    admin_id = _seed_user(db_conn, role="admin")
    sign_in_as(admin_id)

    db_conn.execute(
        "UPDATE room_types SET room_type_name = 'Renamed' WHERE id = %s",
        (room_type_row[0],),
    )

    row = db_conn.execute(
        "SELECT room_type_name FROM room_types WHERE id = %s", (room_type_row[0],)
    ).fetchone()
    assert row == ("Renamed",)


def test_admin_cannot_delete_hotel(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """No role but service_role holds a DELETE grant — admin's write access
    is INSERT/UPDATE only, matching the "no delete yet" decision this
    migration ships under."""
    hotel_id = _seed_hotel(db_conn)
    admin_id = _seed_user(db_conn, role="admin")
    sign_in_as(admin_id)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute("DELETE FROM hotels WHERE id = %s", (hotel_id,))


def test_inactive_admin_cannot_insert_hotel(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """current_app_role() resolves to NULL for an inactive row (migration
    0010), so a deactivated admin loses write access exactly like they lose
    admin-only read access elsewhere — not just at the app layer."""
    row = db_conn.execute(
        "INSERT INTO auth.users DEFAULT VALUES RETURNING id"
    ).fetchone()
    assert row is not None
    user_id = str(row[0])
    db_conn.execute(
        "INSERT INTO app_users (id, full_name, app_role, is_active) "
        "VALUES (%s, 'Deactivated Admin', 'admin', false)",
        (user_id,),
    )
    sign_in_as(user_id)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute("INSERT INTO hotels (hotel_name) VALUES ('New Hotel')")


def test_rls_denies_anon_on_hotels(db_conn: psycopg.Connection[Any]) -> None:
    """anon has real Supabase-default schema USAGE on public (see
    tests/supabase_default_acl_baseline.sql), so Postgres resolves the
    table name fine; migration 0001's own REVOKE ALL ON TABLE hotels FROM
    anon is what denies it, raising InsufficientPrivilege, not
    UndefinedTable."""
    _seed_hotel(db_conn)

    db_conn.execute("SET SESSION AUTHORIZATION anon")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute("SELECT * FROM hotels").fetchall()
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


def test_rls_denies_anon_on_room_types(db_conn: psycopg.Connection[Any]) -> None:
    """Same reasoning as test_rls_denies_anon_on_hotels: anon can resolve
    the table name via its schema USAGE grant, and migration 0001's own
    REVOKE ALL ON TABLE room_types FROM anon is what actually denies it."""
    db_conn.execute("SET SESSION AUTHORIZATION anon")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute("SELECT * FROM room_types").fetchall()
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")
