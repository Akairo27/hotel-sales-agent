"""Migration 0038 against a real Postgres: who may reach the price-import
tables -- deny by default, admin-only dashboard access with every write
policy paired with a read policy (CLAUDE.md rule 11), no path from the
dashboard to validated or approved, append-only nights, the agent's
column-scoped read, and the audit trail.

The backend roles' exact grants are also pinned by test_backend_roles.py's
manifest; the constraints and guards themselves by
test_rate_import_schema.py.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from psycopg import sql

from tests.integration._rate_import_seed import (
    act_as,
    advance_rate_import_batch,
    seed_app_user,
    seed_rate_import_batch,
    seed_rate_import_batch_in,
    seed_rate_import_night,
    seed_rate_import_row,
    set_rate_import_status,
    validate_rate_import_batch,
)
from tests.integration._seed import seed_hotel_and_room_type, seed_room_type

pytestmark = pytest.mark.usefixtures("db_conn")

_TABLES = ("rate_import_batches", "rate_import_rows", "rate_import_nights")
_ROLES = ("anon", "authenticated", "service_role", "hotel_agent", "hotel_worker")

_COLUMNS_WITH_PRIVILEGE = (
    "SELECT a.attname FROM pg_attribute AS a "
    "WHERE a.attrelid = %(table)s::regclass AND a.attnum > 0 AND NOT a.attisdropped "
    "AND has_column_privilege(%(role)s, a.attrelid, a.attnum, %(privilege)s)"
)
_ROW_INSERT_COLUMNS = {
    "batch_id",
    "hotel_id",
    "room_type_id",
    "period_start",
    "period_end",
    "weekday_price_halalas",
    "weekend_price_halalas",
    "is_closed",
}
_ROW_UPDATE_COLUMNS = (_ROW_INSERT_COLUMNS - {"batch_id", "hotel_id"}) | {"is_excluded"}
# What authenticated may write, per table and command. Reading is whole-table
# on all three; the policies narrow the rows to admins.
_DASHBOARD_WRITE_COLUMNS: dict[tuple[str, str], set[str]] = {
    ("rate_import_batches", "INSERT"): {"hotel_id", "price_type"},
    ("rate_import_batches", "UPDATE"): {
        "status",
        "period_end_inclusive",
        "period_years_confirmed",
    },
    ("rate_import_rows", "INSERT"): _ROW_INSERT_COLUMNS,
    ("rate_import_rows", "UPDATE"): _ROW_UPDATE_COLUMNS,
    ("rate_import_nights", "INSERT"): set(),
    ("rate_import_nights", "UPDATE"): set(),
}
_AUDIT_ROWS = (
    "SELECT column_name, old_value, new_value, changed_by::text FROM audit_log "
    "WHERE table_name = %s AND row_id = %s ORDER BY id"
)


def _reset(conn: psycopg.Connection[Any]) -> None:
    conn.execute("RESET SESSION AUTHORIZATION")
    conn.execute("RESET request.jwt.claim.sub")


def _approver(conn: psycopg.Connection[Any]) -> str:
    """An active admin who can view cost, acting on the backend path."""
    user_id = seed_app_user(conn, role="admin", can_view_cost=True)
    act_as(conn, user_id)
    return user_id


def _approved_batch_with_a_night(
    conn: psycopg.Connection[Any],
) -> tuple[int, int, int]:
    """An approved batch with one row and one night, built on the backend
    path; returns the batch, hotel and room type ids."""
    hotel_id, room_type_id = seed_hotel_and_room_type(conn)
    _approver(conn)
    batch_id = seed_rate_import_batch(conn, hotel_id)
    seed_rate_import_row(conn, batch_id, hotel_id, room_type_id)
    validate_rate_import_batch(conn, batch_id)
    seed_rate_import_night(conn, batch_id, hotel_id, room_type_id)
    set_rate_import_status(conn, batch_id, "approved")
    return batch_id, hotel_id, room_type_id


def _status(conn: psycopg.Connection[Any], batch_id: int) -> str:
    row = conn.execute(
        "SELECT status FROM rate_import_batches WHERE id = %s", (batch_id,)
    ).fetchone()
    assert row is not None
    return str(row[0])


def _columns_with(
    conn: psycopg.Connection[Any], role: str, table: str, privilege: str
) -> set[str]:
    rows = conn.execute(
        _COLUMNS_WITH_PRIVILEGE, {"role": role, "table": table, "privilege": privilege}
    ).fetchall()
    return {str(row[0]) for row in rows}


# --- deny by default -------------------------------------------------------


@pytest.mark.parametrize("table", _TABLES)
def test_rls_is_enabled_and_forced(
    db_conn: psycopg.Connection[Any], table: str
) -> None:
    row = db_conn.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE oid = %s::regclass",
        (table,),
    ).fetchone()

    assert row == (True, True)


@pytest.mark.parametrize("table", _TABLES)
@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("privilege", ["DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"])
def test_no_role_may_delete_from_or_restructure_a_price_import_table(
    db_conn: psycopg.Connection[Any], table: str, role: str, privilege: str
) -> None:
    row = db_conn.execute(
        "SELECT has_table_privilege(%s, %s, %s)", (role, table, privilege)
    ).fetchone()

    assert row == (False,)


@pytest.mark.parametrize("role", _ROLES)
def test_nights_are_append_only_for_every_role(
    db_conn: psycopg.Connection[Any], role: str
) -> None:
    assert _columns_with(db_conn, role, "rate_import_nights", "UPDATE") == set()


@pytest.mark.parametrize("table", _TABLES)
@pytest.mark.parametrize("privilege", ["SELECT", "INSERT", "UPDATE"])
def test_anon_holds_nothing(
    db_conn: psycopg.Connection[Any], table: str, privilege: str
) -> None:
    assert _columns_with(db_conn, "anon", table, privilege) == set()


def test_anon_cannot_read_a_price_import_table(
    db_conn: psycopg.Connection[Any],
) -> None:
    db_conn.execute("SET SESSION AUTHORIZATION anon")
    try:
        for table in _TABLES:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db_conn.execute(
                    sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))
                )
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


@pytest.mark.parametrize(("table", "command"), list(_DASHBOARD_WRITE_COLUMNS))
def test_the_dashboard_writes_only_the_granted_columns(
    db_conn: psycopg.Connection[Any], table: str, command: str
) -> None:
    assert (
        _columns_with(db_conn, "authenticated", table, command)
        == _DASHBOARD_WRITE_COLUMNS[(table, command)]
    )


def test_every_dashboard_write_policy_has_a_paired_select_policy(
    db_conn: psycopg.Connection[Any],
) -> None:
    """CLAUDE.md rule 11, for authenticated on these three tables: the same
    check test_backend_roles.py makes for the backend roles."""
    rows = db_conn.execute(
        "SELECT tablename, cmd FROM pg_policies WHERE schemaname = 'public' "
        "AND tablename = ANY(%s) AND 'authenticated' = ANY(roles)",
        (list(_TABLES),),
    ).fetchall()
    commands: dict[str, set[str]] = {}
    for table, command in rows:
        commands.setdefault(table, set()).add(command)

    assert commands == {
        "rate_import_batches": {"SELECT", "INSERT", "UPDATE"},
        "rate_import_rows": {"SELECT", "INSERT", "UPDATE"},
        "rate_import_nights": {"SELECT"},
    }


# --- the dashboard, as an admin ---------------------------------------------


def test_an_admin_builds_and_edits_a_draft(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """Every write here finds its row through the paired SELECT policy: a
    missing one would make each UPDATE match zero rows without an error."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin = seed_app_user(db_conn, role="admin")
    sign_in_as(admin)

    batch_id = seed_rate_import_batch(db_conn, hotel_id)
    row_id = seed_rate_import_row(db_conn, batch_id, hotel_id, room_type_id)
    edited_rows = db_conn.execute(
        "UPDATE rate_import_rows SET weekday_price_halalas = 45000 WHERE id = %s",
        (row_id,),
    ).rowcount
    edited_batches = db_conn.execute(
        "UPDATE rate_import_batches SET period_end_inclusive = true, "
        "period_years_confirmed = true WHERE id = %s",
        (batch_id,),
    ).rowcount

    assert (edited_rows, edited_batches) == (1, 1)
    assert db_conn.execute(
        "SELECT b.created_by::text, r.created_by::text, r.weekday_price_halalas "
        "FROM rate_import_batches AS b "
        "JOIN rate_import_rows AS r ON r.batch_id = b.id WHERE b.id = %s",
        (batch_id,),
    ).fetchone() == (admin, admin, 45_000)


@pytest.mark.parametrize(
    ("from_status", "to_status", "by_column"),
    [
        ("draft", "rejected", "rejected_by"),
        ("validated", "rejected", "rejected_by"),
        ("approved", "disabled", "disabled_by"),
    ],
)
def test_an_admin_rejects_and_disables_in_their_own_name(
    db_conn: psycopg.Connection[Any],
    sign_in_as: Callable[[str], None],
    from_status: str,
    to_status: str,
    by_column: str,
) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)
    batch_id = seed_rate_import_batch_in(db_conn, hotel_id, from_status)
    admin = seed_app_user(db_conn, role="admin")
    sign_in_as(admin)

    set_rate_import_status(db_conn, batch_id, to_status)

    row = db_conn.execute(
        sql.SQL(
            "SELECT status, {}::text FROM rate_import_batches WHERE id = %s"
        ).format(sql.Identifier(by_column)),
        (batch_id,),
    ).fetchone()
    assert row == (to_status, admin)


def test_an_admin_returns_a_validated_batch_to_draft(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)
    batch_id = seed_rate_import_batch_in(db_conn, hotel_id, "validated")
    sign_in_as(seed_app_user(db_conn, role="admin"))

    set_rate_import_status(db_conn, batch_id, "draft")

    assert _status(db_conn, batch_id) == "draft"


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [("draft", "validated"), ("validated", "approved"), ("disabled", "approved")],
)
def test_the_dashboard_has_no_path_to_validated_or_approved(
    db_conn: psycopg.Connection[Any],
    sign_in_as: Callable[[str], None],
    from_status: str,
    to_status: str,
) -> None:
    """Even for an admin the guard itself would let approve: validation and
    approval are the backend's (PR 2), not a dashboard write."""
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    approver = _approver(db_conn)
    batch_id = seed_rate_import_batch_in(db_conn, hotel_id, from_status)
    sign_in_as(approver)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        set_rate_import_status(db_conn, batch_id, to_status)

    _reset(db_conn)
    assert _status(db_conn, batch_id) == from_status


def test_the_dashboard_cannot_write_a_stamp(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    approver = _approver(db_conn)
    batch_id = seed_rate_import_batch(db_conn, hotel_id)
    sign_in_as(approver)

    for column, value in [
        ("validated_fingerprint", "a" * 64),
        ("approved_by", approver),
        ("approval_seq", 1),
        ("created_by", approver),
    ]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute(
                sql.SQL("UPDATE rate_import_batches SET {} = %s WHERE id = %s").format(
                    sql.Identifier(column)
                ),
                (value, batch_id),
            )


def test_the_dashboard_cannot_write_change_or_remove_a_night(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    approver = _approver(db_conn)
    batch_id = seed_rate_import_batch_in(db_conn, hotel_id, "validated")
    sign_in_as(approver)

    for statement in [
        "INSERT INTO rate_import_nights (batch_id, hotel_id, room_type_id, "
        "stay_date, sell_price_halalas) VALUES (%(batch)s, 1, 1, '2027-01-01', 1)",
        "UPDATE rate_import_nights SET sell_price_halalas = 1 "
        "WHERE batch_id = %(batch)s",
        "DELETE FROM rate_import_nights WHERE batch_id = %(batch)s",
        "DELETE FROM rate_import_rows WHERE batch_id = %(batch)s",
        "DELETE FROM rate_import_batches WHERE id = %(batch)s",
    ]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute(statement, {"batch": batch_id})


# --- the dashboard, as anyone else ------------------------------------------


def _signed_in_non_admin(conn: psycopg.Connection[Any], kind: str) -> str:
    """A signed-in identity that is not an active admin: an active sales
    user, a deactivated admin, or one with no app_users row at all."""
    if kind == "unprovisioned":
        row = conn.execute(
            "INSERT INTO auth.users DEFAULT VALUES RETURNING id"
        ).fetchone()
        assert row is not None
        return str(row[0])
    if kind == "deactivated admin":
        return seed_app_user(conn, role="admin", can_view_cost=True, is_active=False)
    return seed_app_user(conn, role="sales", can_view_cost=True)


@pytest.mark.parametrize("kind", ["sales", "deactivated admin", "unprovisioned"])
def test_anyone_but_an_active_admin_sees_and_writes_nothing(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None], kind: str
) -> None:
    batch_id, hotel_id, _ = _approved_batch_with_a_night(db_conn)
    draft_id = seed_rate_import_batch(db_conn, hotel_id)
    sign_in_as(_signed_in_non_admin(db_conn, kind))

    for table in _TABLES:
        assert db_conn.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
        ).fetchone() == (0,)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        seed_rate_import_batch(db_conn, hotel_id)
    disabled = db_conn.execute(
        "UPDATE rate_import_batches SET status = 'disabled' WHERE id = %s", (batch_id,)
    ).rowcount
    rejected = db_conn.execute(
        "UPDATE rate_import_batches SET status = 'rejected' WHERE id = %s", (draft_id,)
    ).rowcount

    _reset(db_conn)
    assert (disabled, rejected) == (0, 0)
    assert (_status(db_conn, batch_id), _status(db_conn, draft_id)) == (
        "approved",
        "draft",
    )


# --- service_role and the agent -----------------------------------------------


def test_service_role_cannot_change_or_remove_what_is_recorded(
    db_conn: psycopg.Connection[Any],
) -> None:
    batch_id, _, _ = _approved_batch_with_a_night(db_conn)
    db_conn.execute("SET SESSION AUTHORIZATION service_role")
    try:
        for statement in [
            "UPDATE rate_import_nights SET sell_price_halalas = 1 WHERE batch_id = %s",
            "DELETE FROM rate_import_nights WHERE batch_id = %s",
            "DELETE FROM rate_import_rows WHERE batch_id = %s",
            "DELETE FROM rate_import_batches WHERE id = %s",
        ]:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db_conn.execute(statement, (batch_id,))
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


def test_the_guards_bind_service_role_too(db_conn: psycopg.Connection[Any]) -> None:
    batch_id, hotel_id, room_type_id = _approved_batch_with_a_night(db_conn)
    db_conn.execute("SET SESSION AUTHORIZATION service_role")
    try:
        with pytest.raises(psycopg.errors.RaiseException, match="is validated"):
            db_conn.execute(
                "INSERT INTO rate_import_nights (batch_id, hotel_id, room_type_id, "
                "stay_date, sell_price_halalas) VALUES (%s, %s, %s, '2027-03-01', 1)",
                (batch_id, hotel_id, room_type_id),
            )
        with pytest.raises(psycopg.errors.RaiseException, match="cannot go from"):
            set_rate_import_status(db_conn, batch_id, "draft")
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


def test_the_agent_reads_batch_order_and_nights(
    db_conn: psycopg.Connection[Any], agent_database_url: str
) -> None:
    batch_id, hotel_id, room_type_id = _approved_batch_with_a_night(db_conn)

    with psycopg.connect(agent_database_url, autocommit=True) as agent:
        batches = agent.execute(
            "SELECT id, hotel_id, status, approval_seq IS NOT NULL "
            "FROM rate_import_batches"
        ).fetchall()
        nights = agent.execute(
            "SELECT batch_id, hotel_id, room_type_id, sell_price_halalas "
            "FROM rate_import_nights"
        ).fetchall()

    assert batches == [(batch_id, hotel_id, "approved", True)]
    assert nights == [(batch_id, hotel_id, room_type_id, 40_000)]


def test_the_agent_reads_nothing_else_and_writes_nothing(
    db_conn: psycopg.Connection[Any], agent_database_url: str
) -> None:
    batch_id, _, _ = _approved_batch_with_a_night(db_conn)

    with psycopg.connect(agent_database_url, autocommit=True) as agent:
        for statement in [
            "SELECT created_by FROM rate_import_batches WHERE id = %(batch)s",
            "SELECT approved_by FROM rate_import_batches WHERE id = %(batch)s",
            "SELECT id FROM rate_import_rows WHERE batch_id = %(batch)s",
            "UPDATE rate_import_batches SET status = 'disabled' WHERE id = %(batch)s",
            "INSERT INTO rate_import_nights (batch_id, hotel_id, room_type_id, "
            "stay_date, sell_price_halalas) VALUES (%(batch)s, 1, 1, '2027-03-01', 1)",
            "DELETE FROM rate_import_nights WHERE batch_id = %(batch)s",
        ]:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                agent.execute(statement, {"batch": batch_id})


# --- audit ----------------------------------------------------------------------


def test_review_choices_and_status_changes_are_audited(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    admin = seed_app_user(db_conn, role="admin")
    sign_in_as(admin)
    batch_id = seed_rate_import_batch(db_conn, hotel_id)

    db_conn.execute(
        "UPDATE rate_import_batches SET period_end_inclusive = true, "
        "period_years_confirmed = true WHERE id = %s",
        (batch_id,),
    )
    set_rate_import_status(db_conn, batch_id, "rejected")

    _reset(db_conn)
    assert db_conn.execute(
        _AUDIT_ROWS, ("rate_import_batches", str(batch_id))
    ).fetchall() == [
        ("period_end_inclusive", None, True, admin),
        ("period_years_confirmed", False, True, admin),
        ("status", "draft", "rejected", admin),
    ]


def test_every_step_to_approval_is_audited_in_the_actors_name(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    approver = _approver(db_conn)
    batch_id = seed_rate_import_batch(db_conn, hotel_id)

    advance_rate_import_batch(db_conn, batch_id, "disabled")

    statuses = [
        (old, new, actor)
        for column, old, new, actor in db_conn.execute(
            _AUDIT_ROWS, ("rate_import_batches", str(batch_id))
        ).fetchall()
        if column == "status"
    ]
    assert statuses == [
        ("draft", "validated", approver),
        ("validated", "approved", approver),
        ("approved", "disabled", approver),
    ]


def test_a_row_edit_is_audited_per_changed_column_and_an_insert_is_not(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin = seed_app_user(db_conn, role="admin")
    sign_in_as(admin)
    batch_id = seed_rate_import_batch(db_conn, hotel_id)
    row_id = seed_rate_import_row(db_conn, batch_id, hotel_id, room_type_id)

    db_conn.execute(
        "UPDATE rate_import_rows SET weekday_price_halalas = 45000, "
        "weekend_price_halalas = 50000, is_excluded = true WHERE id = %s",
        (row_id,),
    )

    _reset(db_conn)
    assert db_conn.execute(
        _AUDIT_ROWS, ("rate_import_rows", str(row_id))
    ).fetchall() == [
        ("weekday_price_halalas", 40_000, 45_000, admin),
        ("is_excluded", False, True, admin),
    ]


def test_closing_a_row_audits_its_prices_as_removed(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    approver = _approver(db_conn)
    batch_id = seed_rate_import_batch(db_conn, hotel_id)
    row_id = seed_rate_import_row(db_conn, batch_id, hotel_id, room_type_id)

    db_conn.execute(
        "UPDATE rate_import_rows SET is_closed = true, weekday_price_halalas = NULL, "
        "weekend_price_halalas = NULL WHERE id = %s",
        (row_id,),
    )

    assert db_conn.execute(
        _AUDIT_ROWS, ("rate_import_rows", str(row_id))
    ).fetchall() == [
        ("weekday_price_halalas", 40_000, None, approver),
        ("weekend_price_halalas", 50_000, None, approver),
        ("is_closed", False, True, approver),
    ]


@pytest.mark.parametrize(("role", "sees_history"), [("admin", True), ("sales", False)])
def test_every_admin_reads_the_price_import_history_and_sales_does_not(
    db_conn: psycopg.Connection[Any],
    sign_in_as: Callable[[str], None],
    role: str,
    sees_history: bool,
) -> None:
    """The audit_log allow-list (0038): none of these columns is a cost, so
    an admin without can_view_cost reads them too."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    other_room_type = seed_room_type(db_conn, hotel_id, room_type_name="Other")
    _approver(db_conn)
    batch_id = seed_rate_import_batch(db_conn, hotel_id)
    row_id = seed_rate_import_row(db_conn, batch_id, hotel_id, room_type_id)
    db_conn.execute(
        "UPDATE rate_import_rows SET room_type_id = %s, "
        "period_start = '2027-01-02', period_end = '2027-01-12', "
        "weekday_price_halalas = 41000, weekend_price_halalas = 51000, "
        "is_excluded = true WHERE id = %s",
        (other_room_type, row_id),
    )
    db_conn.execute(
        "UPDATE rate_import_rows SET is_closed = true, weekday_price_halalas = NULL, "
        "weekend_price_halalas = NULL WHERE id = %s",
        (row_id,),
    )
    advance_rate_import_batch(db_conn, batch_id, "validated")
    sign_in_as(seed_app_user(db_conn, role=role, can_view_cost=False))

    visible = db_conn.execute(
        "SELECT table_name, column_name FROM audit_log "
        "WHERE table_name IN ('rate_import_batches', 'rate_import_rows')"
    ).fetchall()

    expected = {
        ("rate_import_rows", "room_type_id"),
        ("rate_import_rows", "period_start"),
        ("rate_import_rows", "period_end"),
        ("rate_import_rows", "weekday_price_halalas"),
        ("rate_import_rows", "weekend_price_halalas"),
        ("rate_import_rows", "is_closed"),
        ("rate_import_rows", "is_excluded"),
        ("rate_import_batches", "period_end_inclusive"),
        ("rate_import_batches", "period_years_confirmed"),
        ("rate_import_batches", "status"),
    }
    assert set(visible) == (expected if sees_history else set())
