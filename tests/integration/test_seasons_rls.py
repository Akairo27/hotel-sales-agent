"""Verifies migration 0015's grants and RLS policies on seasons against a
real Postgres instance — see CLAUDE.md rule 3: the DB constraint is the
source of truth, not application discipline.

Read access is open to any active app_users row; writes (INSERT/UPDATE) are
admin-only; DELETE is granted to no role but service_role. Same shape as
tests/integration/test_hotels_room_types_rls.py (migration 0014).
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


def _seed_season(conn: psycopg.Connection[Any], *, is_default: bool = False) -> int:
    row = conn.execute(
        "INSERT INTO seasons "
        "(season_name, calendar_type, start_month, start_day, end_month, end_day, "
        "priority, is_default) "
        "VALUES ('Test Season', 'hijri', 1, 1, 1, 1, 0, %s) RETURNING id",
        (is_default,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_sales_can_select_seasons(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    season_id = _seed_season(db_conn)
    sales_id = _seed_user(db_conn, role="sales")
    sign_in_as(sales_id)

    rows = db_conn.execute("SELECT id FROM seasons").fetchall()
    assert [r[0] for r in rows] == [season_id]


def test_sales_cannot_insert_season(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    sales_id = _seed_user(db_conn, role="sales")
    sign_in_as(sales_id)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute(
            "INSERT INTO seasons "
            "(season_name, calendar_type, start_month, start_day, end_month, end_day) "
            "VALUES ('New Season', 'hijri', 1, 1, 2, 1)"
        )


def test_sales_cannot_update_season(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """Unlike INSERT's WITH CHECK, a denied UPDATE does not raise — the row
    simply isn't in the set current_app_role() = 'admin' USING makes visible
    for the UPDATE, so it matches zero rows silently. Asserting on
    rowcount/unchanged data is the real assertion here, not an exception."""
    season_id = _seed_season(db_conn)
    sales_id = _seed_user(db_conn, role="sales")
    sign_in_as(sales_id)

    cursor = db_conn.execute(
        "UPDATE seasons SET season_name = 'Renamed' WHERE id = %s", (season_id,)
    )
    assert cursor.rowcount == 0

    row = db_conn.execute(
        "SELECT season_name FROM seasons WHERE id = %s", (season_id,)
    ).fetchone()
    assert row == ("Test Season",)


def test_admin_can_insert_season(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    admin_id = _seed_user(db_conn, role="admin")
    sign_in_as(admin_id)

    row = db_conn.execute(
        "INSERT INTO seasons "
        "(season_name, calendar_type, start_month, start_day, end_month, end_day) "
        "VALUES ('New Season', 'hijri', 1, 1, 2, 1) RETURNING id"
    ).fetchone()
    assert row is not None


def test_admin_can_update_season(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    season_id = _seed_season(db_conn)
    admin_id = _seed_user(db_conn, role="admin")
    sign_in_as(admin_id)

    db_conn.execute(
        "UPDATE seasons SET season_name = 'Renamed' WHERE id = %s", (season_id,)
    )

    row = db_conn.execute(
        "SELECT season_name FROM seasons WHERE id = %s", (season_id,)
    ).fetchone()
    assert row == ("Renamed",)


def test_admin_can_reorder_priority(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """The drag-to-reorder UI writes priority via plain UPDATE — covered
    separately from season_name to prove the admin policy isn't accidentally
    column-scoped."""
    season_id = _seed_season(db_conn)
    admin_id = _seed_user(db_conn, role="admin")
    sign_in_as(admin_id)

    db_conn.execute("UPDATE seasons SET priority = 5 WHERE id = %s", (season_id,))

    row = db_conn.execute(
        "SELECT priority FROM seasons WHERE id = %s", (season_id,)
    ).fetchone()
    assert row == (5,)


def test_admin_cannot_delete_season(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """No role but service_role holds a DELETE grant — admin's write access
    is INSERT/UPDATE only, matching the "no delete yet" decision this
    migration ships under (same as hotels/room_types)."""
    season_id = _seed_season(db_conn)
    admin_id = _seed_user(db_conn, role="admin")
    sign_in_as(admin_id)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute("DELETE FROM seasons WHERE id = %s", (season_id,))


def test_inactive_admin_cannot_insert_season(
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
        db_conn.execute(
            "INSERT INTO seasons "
            "(season_name, calendar_type, start_month, start_day, end_month, end_day) "
            "VALUES ('New Season', 'hijri', 1, 1, 2, 1)"
        )


def test_rls_denies_anon_on_seasons(db_conn: psycopg.Connection[Any]) -> None:
    """anon has real Supabase-default schema USAGE on public (see
    tests/supabase_default_acl_baseline.sql), so Postgres resolves the
    table name fine; migration 0002's own REVOKE ALL ON TABLE seasons FROM
    anon (seasons predates migration 0012's schema-wide lockdown) is what
    denies it, raising InsufficientPrivilege, not UndefinedTable."""
    _seed_season(db_conn)

    db_conn.execute("SET SESSION AUTHORIZATION anon")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute("SELECT * FROM seasons").fetchall()
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")
