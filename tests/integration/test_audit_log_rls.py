"""Verifies audit_log is append-only and admin-read-only against a real
Postgres instance — see CLAUDE.md rule 3: the DB constraint is the source
of truth, not application discipline.

Needs docs/phase-3-pr-a/pending-migrations/0010_app_users_and_roles.sql and
0011_audit_log.sql promoted to db/migrations/ first — see
docs/phase-3-pr-a/README.md.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
import pytest

pytestmark = pytest.mark.usefixtures("db_conn")


def _seed_user(
    conn: psycopg.Connection[Any], *, role: str = "sales", can_view_cost: bool = False
) -> str:
    row = conn.execute("INSERT INTO auth.users DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    user_id = str(row[0])
    conn.execute(
        "INSERT INTO app_users (id, full_name, app_role, can_view_cost) "
        "VALUES (%s, 'Test User', %s, %s)",
        (user_id, role, can_view_cost),
    )
    return user_id


def _seed_audit_entry(
    conn: psycopg.Connection[Any],
    changed_by: str,
    *,
    table_name: str = "allotments",
    column_name: str = "cost_per_night",
) -> int:
    row = conn.execute(
        "INSERT INTO audit_log (table_name, row_id, column_name, old_value, "
        "new_value, changed_by) "
        "VALUES (%s, '1', %s, '10000'::jsonb, '11000'::jsonb, %s) RETURNING id",
        (table_name, column_name, changed_by),
    ).fetchone()
    assert row is not None
    return int(row[0])


# migration 0019 gates audit_log per row by (table_name, column_name), not
# by table alone -- see that migration's comment. The single
# test_admin_can_select_audit_log this replaces asserted role gating only;
# these five pin the column-level gate the same migration adds, including
# its two fail-closed guarantees (an unlisted column, and a name collision
# across tables).


def test_admin_without_cost_visibility_sees_non_financial_audit_rows(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """A cost-blind admin must still see a non-financial entry like a role
    change -- migration 0019's gate is per financial column, not per
    table or per role alone."""
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=False)
    _seed_audit_entry(db_conn, admin_id, table_name="app_users", column_name="app_role")
    sign_in_as(admin_id)

    rows = db_conn.execute("SELECT * FROM audit_log").fetchall()
    assert len(rows) == 1


def test_admin_without_cost_visibility_sees_price_override_audit_rows(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """migration 0022: none of price_overrides' three columns reverse-derive
    cost -- they are final prices, not a margin or profit floor over cost
    (migration 0021's own comment) -- so a cost-blind admin must see their
    audit history the same as any other non-financial column."""
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=False)
    _seed_audit_entry(
        db_conn,
        admin_id,
        table_name="price_overrides",
        column_name="ask_price_override",
    )
    _seed_audit_entry(
        db_conn,
        admin_id,
        table_name="price_overrides",
        column_name="min_allowed_override",
    )
    _seed_audit_entry(
        db_conn, admin_id, table_name="price_overrides", column_name="expires_at"
    )
    sign_in_as(admin_id)

    rows = db_conn.execute("SELECT * FROM audit_log").fetchall()
    assert len(rows) == 3


def test_admin_without_cost_visibility_cannot_see_cost_per_night(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """The leak migration 0019 closes: before it, this admin could read
    allotments.cost_per_night's old/new values straight out of audit_log
    despite /allotments itself masking the same number."""
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=False)
    _seed_audit_entry(db_conn, admin_id)  # default: allotments.cost_per_night
    sign_in_as(admin_id)

    rows = db_conn.execute("SELECT * FROM audit_log").fetchall()
    assert rows == []


def test_admin_with_cost_visibility_sees_everything(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    _seed_audit_entry(db_conn, admin_id)  # allotments.cost_per_night
    _seed_audit_entry(db_conn, admin_id, table_name="app_users", column_name="app_role")
    sign_in_as(admin_id)

    rows = db_conn.execute("SELECT * FROM audit_log").fetchall()
    assert len(rows) == 2


def test_admin_without_cost_visibility_cannot_see_an_unlisted_column(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """migration 0019's allow-list fails closed: a column nobody has added
    to it yet -- simulating a future financial column this repository
    hasn't audited before -- stays hidden by default, rather than exposed
    until someone remembers to block it."""
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=False)
    _seed_audit_entry(
        db_conn, admin_id, table_name="price_rules", column_name="some_future_column"
    )
    sign_in_as(admin_id)

    rows = db_conn.execute("SELECT * FROM audit_log").fetchall()
    assert rows == []


def test_admin_without_cost_visibility_cannot_see_allowed_name_on_different_table(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """migration 0019 matches (table_name, column_name) as a pair, not
    column_name alone: a column that happens to share a name with an
    allowed one ("is_active") on a different, hypothetical financial table
    must not leak just because the name matches."""
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=False)
    _seed_audit_entry(
        db_conn, admin_id, table_name="some_future_cost_table", column_name="is_active"
    )
    sign_in_as(admin_id)

    rows = db_conn.execute("SELECT * FROM audit_log").fetchall()
    assert rows == []


def test_sales_user_cannot_select_audit_log(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    admin_id = _seed_user(db_conn, role="admin")
    _seed_audit_entry(db_conn, admin_id)
    sales_id = _seed_user(db_conn, role="sales")
    sign_in_as(sales_id)

    rows = db_conn.execute("SELECT * FROM audit_log").fetchall()
    assert rows == []


def test_anon_cannot_select_audit_log(db_conn: psycopg.Connection[Any]) -> None:
    """anon gets no grant on audit_log at all, same as app_users — see
    test_rls_denies_anon in test_app_users_and_roles_rls.py for why this
    is a table-level permission error, not a missing-schema one."""
    admin_id = _seed_user(db_conn, role="admin")
    _seed_audit_entry(db_conn, admin_id)

    db_conn.execute("SET SESSION AUTHORIZATION anon")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute("SELECT * FROM audit_log").fetchall()
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


def test_audit_log_is_append_only_update_rejected(
    db_conn: psycopg.Connection[Any],
) -> None:
    admin_id = _seed_user(db_conn, role="admin")
    entry_id = _seed_audit_entry(db_conn, admin_id)

    db_conn.execute("SET SESSION AUTHORIZATION service_role")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute(
                "UPDATE audit_log SET new_value = '99'::jsonb WHERE id = %s",
                (entry_id,),
            )
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


def test_audit_log_is_append_only_delete_rejected(
    db_conn: psycopg.Connection[Any],
) -> None:
    admin_id = _seed_user(db_conn, role="admin")
    entry_id = _seed_audit_entry(db_conn, admin_id)

    db_conn.execute("SET SESSION AUTHORIZATION service_role")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute("DELETE FROM audit_log WHERE id = %s", (entry_id,))
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


def test_audit_log_changed_by_must_reference_a_real_app_user(
    db_conn: psycopg.Connection[Any],
) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db_conn.execute(
            "INSERT INTO audit_log (table_name, row_id, column_name, "
            "new_value, changed_by) "
            "VALUES ('allotments', '1', 'cost_per_night', '11000'::jsonb, "
            "gen_random_uuid())"
        )
