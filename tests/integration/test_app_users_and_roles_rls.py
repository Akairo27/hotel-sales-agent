"""Verifies app_users' RLS policies and the current_app_role() /
current_user_can_view_cost() helper functions against a real Postgres
instance — see CLAUDE.md rule 3: the DB constraint is the source of
truth, not application discipline.

Needs docs/phase-3-pr-a/pending-migrations/0010_app_users_and_roles.sql
promoted to db/migrations/ first — see docs/phase-3-pr-a/README.md.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
import pytest

pytestmark = pytest.mark.usefixtures("db_conn")


def _seed_user(
    conn: psycopg.Connection[Any],
    *,
    role: str = "sales",
    can_view_cost: bool = False,
    is_active: bool = True,
) -> str:
    """Inserts one auth.users row and its matching app_users row, returning
    the user id as a string (auth.uid() compares against a session GUC,
    which is text, not uuid, until cast)."""
    row = conn.execute("INSERT INTO auth.users DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    user_id = str(row[0])
    conn.execute(
        "INSERT INTO app_users (id, full_name, app_role, can_view_cost, is_active) "
        "VALUES (%s, 'Test User', %s, %s, %s)",
        (user_id, role, can_view_cost, is_active),
    )
    return user_id


def test_user_can_select_own_row(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    user_id = _seed_user(db_conn)
    sign_in_as(user_id)

    rows = db_conn.execute("SELECT id FROM app_users").fetchall()
    assert [str(r[0]) for r in rows] == [user_id]


def test_sales_user_cannot_select_other_users_row(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    _seed_user(db_conn, role="sales")
    viewer_id = _seed_user(db_conn, role="sales")
    sign_in_as(viewer_id)

    rows = db_conn.execute("SELECT id FROM app_users").fetchall()
    assert [str(r[0]) for r in rows] == [viewer_id]


def test_admin_can_select_all_rows(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    _seed_user(db_conn, role="sales")
    _seed_user(db_conn, role="sales")
    admin_id = _seed_user(db_conn, role="admin")
    sign_in_as(admin_id)

    rows = db_conn.execute("SELECT id FROM app_users").fetchall()
    assert len(rows) == 3


def test_authenticated_has_no_write_grant(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """The self-escalation guard: even a user updating only their own row
    must be rejected at the grant level, since no RLS policy alone can
    stop "update my own role to admin" once UPDATE is granted at all."""
    user_id = _seed_user(db_conn, role="sales")
    sign_in_as(user_id)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute(
            "UPDATE app_users SET app_role = 'admin' WHERE id = %s", (user_id,)
        )


def test_current_app_role_reads_the_signed_in_users_role(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    admin_id = _seed_user(db_conn, role="admin")
    sign_in_as(admin_id)

    row = db_conn.execute("SELECT current_app_role()").fetchone()
    assert row == ("admin",)


def test_current_app_role_ignores_an_inactive_user(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """A deactivated admin must not keep admin-only read access — an
    inactive row resolves to no role at all, not their last-active one."""
    user_id = _seed_user(db_conn, role="admin", is_active=False)
    sign_in_as(user_id)

    row = db_conn.execute("SELECT current_app_role()").fetchone()
    assert row == (None,)


def test_current_user_can_view_cost_defaults_false(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    user_id = _seed_user(db_conn, can_view_cost=False)
    sign_in_as(user_id)

    row = db_conn.execute("SELECT current_user_can_view_cost()").fetchone()
    assert row == (False,)


def test_current_user_can_view_cost_reflects_the_column(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    user_id = _seed_user(db_conn, role="sales", can_view_cost=True)
    sign_in_as(user_id)

    row = db_conn.execute("SELECT current_user_can_view_cost()").fetchone()
    assert row == (True,)


def test_rls_denies_anon(db_conn: psycopg.Connection[Any]) -> None:
    """anon gets no grant on app_users at all (0010 only grants authenticated).
    anon does have schema USAGE — tests/supabase_default_acl_baseline.sql
    simulates the real Supabase default, granted independently of this
    schema's own migrations — so name resolution succeeds and this fails
    at the table-level permission check instead, same as every other
    table in this schema."""
    _seed_user(db_conn)

    db_conn.execute("SET SESSION AUTHORIZATION anon")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute("SELECT * FROM app_users").fetchall()
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


def test_app_users_role_rejects_unknown_value(db_conn: psycopg.Connection[Any]) -> None:
    row = db_conn.execute(
        "INSERT INTO auth.users DEFAULT VALUES RETURNING id"
    ).fetchone()
    assert row is not None

    with pytest.raises(psycopg.errors.CheckViolation, match="app_users_role_valid"):
        db_conn.execute(
            "INSERT INTO app_users (id, full_name, app_role) VALUES (%s, 'X', 'owner')",
            (row[0],),
        )


def test_app_users_id_cascades_from_auth_users_delete(
    db_conn: psycopg.Connection[Any],
) -> None:
    user_id = _seed_user(db_conn)

    db_conn.execute("DELETE FROM auth.users WHERE id = %s", (user_id,))

    row = db_conn.execute(
        "SELECT count(*) FROM app_users WHERE id = %s", (user_id,)
    ).fetchone()
    assert row == (0,)
