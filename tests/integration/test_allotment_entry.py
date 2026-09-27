"""Migration 0029: admin_set_allotments, its dedicated owner role
(allotment_entry_writer), the audit changes, and room_night_availability_
for_dashboard — see docs/plans/manual-entry.md section D2/D6 and PR-2.

Real Postgres throughout (db_conn / sign_in_as), same as
test_allotments_rls.py — RLS and SECURITY DEFINER hardening are not
provable against a mock.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg import sql

from services.inventory.errors import InsufficientInventoryError
from services.inventory.operations import create_hold
from tests.integration._seed import (
    seed_actor,
    seed_allotment_nights,
    seed_hotel_and_room_type,
)

pytestmark = pytest.mark.usefixtures("db_conn")

_OWNER_ROLE = "allotment_entry_writer"
_FUNCTION_SIGNATURE = (
    "admin_set_allotments(bigint, bigint, date, date, integer, bigint, boolean)"
)
_THREAD_JOIN_TIMEOUT_S = 10

# Mirrors migration 0029's grants exactly, the same way test_backend_roles.py's
# _MANIFEST mirrors migration 0027's.
_OWNER_MANIFEST: dict[str, dict[str, tuple[str, ...]]] = {
    "hotels": {"SELECT": ("id",)},
    "room_types": {"SELECT": ("id", "hotel_id")},
    "allotments": {
        "SELECT": (
            "id",
            "hotel_id",
            "room_type_id",
            "stay_date",
            "total_rooms",
            "cost_per_night",
        ),
        "INSERT": (
            "hotel_id",
            "room_type_id",
            "stay_date",
            "total_rooms",
            "cost_per_night",
        ),
        "UPDATE": ("total_rooms", "cost_per_night"),
    },
    "room_night_inventory": {
        "SELECT": ("allotment_id", "stay_date", "total", "reserved", "held"),
        "INSERT": ("allotment_id", "stay_date", "total"),
        "UPDATE": ("total",),
    },
}
_OWNER_EXECUTABLE_FUNCTIONS = {
    # The owner of a function always implicitly has EXECUTE on it,
    # regardless of any explicit grant — confirmed against real Postgres,
    # not assumed.
    "admin_set_allotments",
    "current_app_role",
    "current_user_can_view_cost",
    "allotment_entry_riyadh_date",
}


def _seed_user(
    conn: psycopg.Connection[Any],
    *,
    role: str,
    can_view_cost: bool,
    is_active: bool = True,
) -> str:
    row = conn.execute("INSERT INTO auth.users DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    user_id = str(row[0])
    conn.execute(
        "INSERT INTO app_users (id, full_name, app_role, can_view_cost, is_active) "
        "VALUES (%s, 'Test User', %s, %s, %s)",
        (user_id, role, can_view_cost, is_active),
    )
    return user_id


def _riyadh_today(conn: psycopg.Connection[Any]) -> date:
    """Reads "today" the same way admin_set_allotments does, inside the
    same session, so behavioral tests never race the real wall clock —
    the plan's own words for this: "the expected dates computed inside
    the same transaction"."""
    row = conn.execute("SELECT allotment_entry_riyadh_date(now())").fetchone()
    assert row is not None
    result: date = row[0]
    return result


def _reset_to_full_privilege(conn: psycopg.Connection[Any]) -> None:
    conn.execute("RESET SESSION AUTHORIZATION")
    conn.execute("RESET request.jwt.claim.sub")


# ---------------------------------------------------------------------------
# Hardening
# ---------------------------------------------------------------------------


def test_search_path_is_empty(db_conn: psycopg.Connection[Any]) -> None:
    row = db_conn.execute(
        "SELECT proconfig FROM pg_proc WHERE proname = 'admin_set_allotments'"
    ).fetchone()
    # Postgres stores SET search_path = '' as the quoted empty string, not
    # a bare trailing "=" (confirmed against real Postgres, not assumed).
    assert row == (['search_path=""'],)


def test_decoy_object_in_callers_search_path_is_ignored(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """A caller-controlled search_path must never change which now() the
    function actually reads — proven behaviorally, not just by reading
    proconfig. A fixed, unambiguously-past date (not "yesterday" relative
    to the real clock) means this test needs no buffer against clock skew
    between the test runner and the database server.
    """
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)

    db_conn.execute("CREATE SCHEMA IF NOT EXISTS decoy")
    db_conn.execute(
        "CREATE OR REPLACE FUNCTION decoy.now() RETURNS timestamptz "
        "LANGUAGE sql AS $$ SELECT '1999-01-01T00:00:00+03'::timestamptz $$"
    )
    sign_in_as(admin_id)
    db_conn.execute("SET search_path = decoy, public")
    try:
        # Under decoy.now() (1999), 2020-01-01 would look far in the
        # future rather than years in the past, and this call would
        # wrongly succeed instead of being rejected.
        with pytest.raises(psycopg.errors.RaiseException, match="before today"):
            db_conn.execute(
                "SELECT * FROM admin_set_allotments(%s, %s, %s, %s, 1, 10000, false)",
                (hotel_id, room_type_id, date(2020, 1, 1), date(2020, 1, 2)),
            )
    finally:
        db_conn.execute("RESET search_path")


def test_owner_role_attributes(db_conn: psycopg.Connection[Any]) -> None:
    row = db_conn.execute(
        "SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, "
        "rolreplication, rolinherit, rolvaliduntil FROM pg_roles WHERE rolname = %s",
        (_OWNER_ROLE,),
    ).fetchone()
    assert row == (False, False, False, False, False, False, False, None)


def test_owner_role_is_a_member_of_nothing_and_owns_only_its_own_functions(
    db_conn: psycopg.Connection[Any],
) -> None:
    members = db_conn.execute(
        "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
        "WHERE r.rolname = %s",
        (_OWNER_ROLE,),
    ).fetchone()
    assert members == (0,)
    owned = db_conn.execute(
        "SELECT p.proname FROM pg_proc p "
        "WHERE p.proowner = (SELECT oid FROM pg_roles WHERE rolname = %s)",
        (_OWNER_ROLE,),
    ).fetchall()
    assert {r[0] for r in owned} == {"admin_set_allotments"}


def _owner_column_privileges(
    conn: psycopg.Connection[Any],
) -> set[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT c.relname, a.attname, p.privilege FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid = c.oid "
        "AND a.attnum > 0 AND NOT a.attisdropped "
        "CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('REFERENCES')) "
        "AS p(privilege) "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm', 'p') "
        "AND has_column_privilege(%s::name, c.oid, a.attnum, p.privilege)",
        (_OWNER_ROLE,),
    ).fetchall()
    return {(t, c, p) for t, c, p in rows}


def _owner_expected_privileges() -> set[tuple[str, str, str]]:
    return {
        (table, column, privilege)
        for table, privileges in _OWNER_MANIFEST.items()
        for privilege, columns in privileges.items()
        for column in columns
    }


def test_owner_role_privileges_match_the_manifest_exactly(
    db_conn: psycopg.Connection[Any],
) -> None:
    actual = _owner_column_privileges(db_conn)
    expected = _owner_expected_privileges()
    assert actual == expected, (
        f"beyond manifest: {sorted(actual - expected)}, "
        f"missing: {sorted(expected - actual)}"
    )


def test_owner_role_can_never_delete_truncate_or_trigger(
    db_conn: psycopg.Connection[Any],
) -> None:
    for privilege in ("DELETE", "TRUNCATE", "TRIGGER"):
        rows = db_conn.execute(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm', 'p') "
            "AND has_table_privilege(%s::name, c.oid, %s)",
            (_OWNER_ROLE, privilege),
        ).fetchall()
        assert rows == [], f"{privilege}: {rows}"


def test_owner_role_executes_only_the_functions_it_needs(
    db_conn: psycopg.Connection[Any],
) -> None:
    rows = db_conn.execute(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.prokind = 'f' "
        "AND has_function_privilege(%s::name, p.oid, 'EXECUTE')",
        (_OWNER_ROLE,),
    ).fetchall()
    assert {r[0] for r in rows} == _OWNER_EXECUTABLE_FUNCTIONS


def test_owner_role_has_no_sequence_or_schema_creation_privileges(
    db_conn: psycopg.Connection[Any],
) -> None:
    sequences = db_conn.execute(
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'S' AND ("
        "has_sequence_privilege(%(r)s::name, c.oid, 'USAGE') "
        "OR has_sequence_privilege(%(r)s::name, c.oid, 'SELECT') "
        "OR has_sequence_privilege(%(r)s::name, c.oid, 'UPDATE'))",
        {"r": _OWNER_ROLE},
    ).fetchall()
    assert sequences == []
    schema = db_conn.execute(
        "SELECT has_schema_privilege(%(r)s::name, 'public', 'USAGE'), "
        "has_schema_privilege(%(r)s::name, 'public', 'CREATE')",
        {"r": _OWNER_ROLE},
    ).fetchone()
    assert schema == (True, False)


def test_function_acl_is_owner_and_authenticated_only(
    db_conn: psycopg.Connection[Any],
) -> None:
    for grantee, expected in (
        ("public", False),
        ("anon", False),
        ("authenticated", True),
        (_OWNER_ROLE, True),
    ):
        has_execute = db_conn.execute(
            "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
            (grantee, _FUNCTION_SIGNATURE),
        ).fetchone()
        assert has_execute == (expected,), f"{grantee}: expected EXECUTE={expected}"


# ---------------------------------------------------------------------------
# Actor resolution — the live auth.uid() on hotel-sales-agent-dev (read
# from pg_proc, not assumed) tries request.jwt.claim.sub first, then falls
# back to parsing request.jwt.claims (a JSON blob), because newer
# PostgREST versions set only the latter. admin_set_allotments inlines
# that exact expression rather than calling auth.uid() (see the
# migration's own comment on why); both paths need their own proof, or a
# PostgREST upgrade that stops sending the flat GUC would make every real
# call resolve to no actor and get rejected.
# ---------------------------------------------------------------------------


def _changed_by_for(
    db_conn: psycopg.Connection[Any], hotel_id: int, room_type_id: int
) -> str:
    allotment_row = db_conn.execute(
        "SELECT id FROM allotments WHERE hotel_id = %s AND room_type_id = %s",
        (hotel_id, room_type_id),
    ).fetchone()
    assert allotment_row is not None
    changed_by_row = db_conn.execute(
        "SELECT changed_by::text FROM audit_log "
        "WHERE table_name = 'allotments' AND row_id = %s LIMIT 1",
        (str(allotment_row[0]),),
    ).fetchone()
    assert changed_by_row is not None
    result: str = changed_by_row[0]
    return result


def test_actor_resolves_from_the_flat_sub_claim_alone(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """sign_in_as (tests/conftest.py) sets only request.jwt.claim.sub,
    never request.jwt.claims — exactly the older-PostgREST shape."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    start = _riyadh_today(db_conn) + timedelta(days=10)
    sign_in_as(admin_id)

    rows = db_conn.execute(
        "SELECT out_action FROM admin_set_allotments(%s, %s, %s, %s, 2, 8000, false)",
        (hotel_id, room_type_id, start, start + timedelta(days=1)),
    ).fetchall()
    assert rows == [("created",)]

    _reset_to_full_privilege(db_conn)
    assert _changed_by_for(db_conn, hotel_id, room_type_id) == admin_id


def test_actor_resolves_from_json_claims_when_the_flat_sub_is_unset(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The fallback path newer PostgREST actually uses in production —
    without it, admin_set_allotments would reject every real dashboard
    call, exactly as it did before this expression was corrected against
    the live auth.uid() definition."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    start = _riyadh_today(db_conn) + timedelta(days=10)

    db_conn.execute("SET SESSION AUTHORIZATION authenticated")
    db_conn.execute(
        sql.SQL("SET request.jwt.claims = {}").format(
            sql.Literal(json.dumps({"sub": admin_id}))
        )
    )
    try:
        rows = db_conn.execute(
            "SELECT out_action FROM "
            "admin_set_allotments(%s, %s, %s, %s, 2, 8000, false)",
            (hotel_id, room_type_id, start, start + timedelta(days=1)),
        ).fetchall()
        assert rows == [("created",)]
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")
        db_conn.execute("RESET request.jwt.claims")

    assert _changed_by_for(db_conn, hotel_id, room_type_id) == admin_id


# ---------------------------------------------------------------------------
# Riyadh boundary (D4) — the internal helper directly, no wall-clock
# dependency at all.
# ---------------------------------------------------------------------------


def test_riyadh_date_helper_boundary(db_conn: psycopg.Connection[Any]) -> None:
    # 00:00:00 in Asia/Riyadh (+03, no DST) is 21:00:00 UTC the day before.
    just_before = datetime(2026, 6, 14, 20, 59, 59, tzinfo=UTC)
    just_after = datetime(2026, 6, 14, 21, 0, 0, tzinfo=UTC)
    before = db_conn.execute(
        "SELECT allotment_entry_riyadh_date(%s)", (just_before,)
    ).fetchone()
    after = db_conn.execute(
        "SELECT allotment_entry_riyadh_date(%s)", (just_after,)
    ).fetchone()
    assert before == (date(2026, 6, 14),)
    assert after == (date(2026, 6, 15),)


def test_todays_riyadh_night_is_accepted_yesterdays_is_rejected(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    today = _riyadh_today(db_conn)
    yesterday = today - timedelta(days=1)

    sign_in_as(admin_id)
    rows = db_conn.execute(
        "SELECT out_action FROM admin_set_allotments(%s, %s, %s, %s, 1, 10000, false)",
        (hotel_id, room_type_id, today, today + timedelta(days=1)),
    ).fetchall()
    assert rows == [("created",)]

    with pytest.raises(psycopg.errors.RaiseException, match="before today"):
        db_conn.execute(
            "SELECT * FROM admin_set_allotments(%s, %s, %s, %s, 1, 10000, false)",
            (hotel_id, room_type_id, yesterday, today),
        )


# ---------------------------------------------------------------------------
# Authorization matrix (D3)
# ---------------------------------------------------------------------------


def test_admin_with_cost_visibility_can_enter_allotments(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    start = _riyadh_today(db_conn) + timedelta(days=10)
    sign_in_as(admin_id)

    rows = db_conn.execute(
        "SELECT out_action FROM admin_set_allotments(%s, %s, %s, %s, 4, 12000, false)",
        (hotel_id, room_type_id, start, start + timedelta(days=3)),
    ).fetchall()
    assert rows == [("created",), ("created",), ("created",)]


@pytest.mark.parametrize(
    ("role", "can_view_cost", "is_active"),
    [
        pytest.param("admin", False, True, id="admin-without-cost-visibility"),
        pytest.param("sales", True, True, id="sales"),
        pytest.param("admin", True, False, id="inactive-admin"),
    ],
)
def test_unauthorized_users_cannot_enter_allotments(
    db_conn: psycopg.Connection[Any],
    sign_in_as: Callable[[str], None],
    role: str,
    can_view_cost: bool,
    is_active: bool,
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    user_id = _seed_user(
        db_conn, role=role, can_view_cost=can_view_cost, is_active=is_active
    )
    start = _riyadh_today(db_conn) + timedelta(days=10)
    sign_in_as(user_id)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute(
            "SELECT * FROM admin_set_allotments(%s, %s, %s, %s, 4, 12000, false)",
            (hotel_id, room_type_id, start, start + timedelta(days=1)),
        )

    _reset_to_full_privilege(db_conn)
    count = db_conn.execute("SELECT count(*) FROM allotments").fetchone()
    assert count == (0,)


def test_anon_has_no_execute_at_all(db_conn: psycopg.Connection[Any]) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    start = _riyadh_today(db_conn) + timedelta(days=10)
    db_conn.execute("SET SESSION AUTHORIZATION anon")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute(
                "SELECT * FROM admin_set_allotments(%s, %s, %s, %s, 1, 10000, false)",
                (hotel_id, room_type_id, start, start + timedelta(days=1)),
            )
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


# ---------------------------------------------------------------------------
# Behavior
# ---------------------------------------------------------------------------


def test_unchanged_on_identical_resubmit(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    start = _riyadh_today(db_conn) + timedelta(days=10)
    sign_in_as(admin_id)

    db_conn.execute(
        "SELECT * FROM admin_set_allotments(%s, %s, %s, %s, 4, 12000, false)",
        (hotel_id, room_type_id, start, start + timedelta(days=2)),
    )
    rows = db_conn.execute(
        "SELECT out_action FROM admin_set_allotments(%s, %s, %s, %s, 4, 12000, false)",
        (hotel_id, room_type_id, start, start + timedelta(days=2)),
    ).fetchall()
    assert rows == [("unchanged",), ("unchanged",)]


def test_update_changes_total_and_cost(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    start = _riyadh_today(db_conn) + timedelta(days=10)
    seed_allotment_nights(
        db_conn, hotel_id, room_type_id, start, nights=2, total_rooms=4
    )
    sign_in_as(admin_id)

    rows = db_conn.execute(
        "SELECT out_action, out_total_rooms, out_cost_per_night "
        "FROM admin_set_allotments(%s, %s, %s, %s, 6, 15000, false)",
        (hotel_id, room_type_id, start, start + timedelta(days=2)),
    ).fetchall()
    assert rows == [("updated", 6, 15000), ("updated", 6, 15000)]

    _reset_to_full_privilege(db_conn)
    stored = db_conn.execute(
        "SELECT total_rooms, cost_per_night FROM allotments "
        "WHERE hotel_id = %s AND room_type_id = %s ORDER BY stay_date",
        (hotel_id, room_type_id),
    ).fetchall()
    assert stored == [(6, 15000), (6, 15000)]
    inventory = db_conn.execute(
        "SELECT total FROM room_night_inventory rni "
        "JOIN allotments a ON a.id = rni.allotment_id "
        "WHERE a.hotel_id = %s AND a.room_type_id = %s ORDER BY rni.stay_date",
        (hotel_id, room_type_id),
    ).fetchall()
    assert inventory == [(6,), (6,)]


def test_reduction_below_reserved_or_held_is_refused_and_nothing_in_the_range_changes(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """The second of three nights is already fully booked; the whole call
    must roll back, not just that one night — proving admin_set_allotments'
    own writes to the first (already-processed) night are undone too, the
    same as any other uncaught exception inside a SECURITY DEFINER call."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    start = _riyadh_today(db_conn) + timedelta(days=10)
    seed_allotment_nights(
        db_conn, hotel_id, room_type_id, start, nights=3, total_rooms=5
    )
    db_conn.execute(
        "UPDATE room_night_inventory SET reserved = 5 WHERE allotment_id = ("
        "SELECT id FROM allotments WHERE hotel_id = %s AND room_type_id = %s "
        "AND stay_date = %s)",
        (hotel_id, room_type_id, start + timedelta(days=1)),
    )
    sign_in_as(admin_id)

    with pytest.raises(psycopg.errors.RaiseException, match="cannot reduce rooms"):
        db_conn.execute(
            "SELECT * FROM admin_set_allotments(%s, %s, %s, %s, 2, 8000, false)",
            (hotel_id, room_type_id, start, start + timedelta(days=3)),
        )

    _reset_to_full_privilege(db_conn)
    unchanged = db_conn.execute(
        "SELECT total_rooms, cost_per_night FROM allotments "
        "WHERE hotel_id = %s AND room_type_id = %s ORDER BY stay_date",
        (hotel_id, room_type_id),
    ).fetchall()
    assert unchanged == [(5, 10_000), (5, 10_000), (5, 10_000)]


def test_dry_run_reports_the_same_actions_as_a_real_run_and_writes_nothing(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    start = _riyadh_today(db_conn) + timedelta(days=10)
    seed_allotment_nights(
        db_conn, hotel_id, room_type_id, start, nights=1, total_rooms=4
    )
    sign_in_as(admin_id)

    dry_run_rows = db_conn.execute(
        "SELECT out_stay_date, out_action, out_total_rooms "
        "FROM admin_set_allotments(%s, %s, %s, %s, 7, 9000, true) "
        "ORDER BY out_stay_date",
        (hotel_id, room_type_id, start, start + timedelta(days=2)),
    ).fetchall()
    real_rows = db_conn.execute(
        "SELECT out_stay_date, out_action, out_total_rooms "
        "FROM admin_set_allotments(%s, %s, %s, %s, 7, 9000, false) "
        "ORDER BY out_stay_date",
        (hotel_id, room_type_id, start, start + timedelta(days=2)),
    ).fetchall()
    assert (
        dry_run_rows
        == real_rows
        == [
            (start, "updated", 7),
            (start + timedelta(days=1), "created", 7),
        ]
    )

    _reset_to_full_privilege(db_conn)
    count = db_conn.execute(
        "SELECT count(*) FROM allotments WHERE hotel_id = %s AND room_type_id = %s",
        (hotel_id, room_type_id),
    ).fetchone()
    assert count == (
        2,
    )  # only the real run's writes: the original night plus one new one


def test_repairs_an_allotment_missing_its_room_night_inventory_row(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """The production database holds exactly this: one allotments row with
    zero room_night_inventory rows (docs/plans/manual-entry.md section 7).
    Re-entering that night must close the gap, not misread NULL
    reserved/held as "0 < 0" and skip the reduction check incorrectly."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    start = _riyadh_today(db_conn) + timedelta(days=10)
    seed_actor(db_conn)  # a raw INSERT here needs app.actor_id set too
    db_conn.execute(
        "INSERT INTO allotments "
        "(hotel_id, room_type_id, stay_date, total_rooms, cost_per_night) "
        "VALUES (%s, %s, %s, 3, 9000)",
        (hotel_id, room_type_id, start),
    )
    sign_in_as(admin_id)

    rows = db_conn.execute(
        "SELECT out_action, out_reserved, out_held "
        "FROM admin_set_allotments(%s, %s, %s, %s, 5, 9500, false)",
        (hotel_id, room_type_id, start, start + timedelta(days=1)),
    ).fetchall()
    assert rows == [("updated", 0, 0)]

    _reset_to_full_privilege(db_conn)
    inventory = db_conn.execute(
        "SELECT total, reserved, held FROM room_night_inventory rni "
        "JOIN allotments a ON a.id = rni.allotment_id "
        "WHERE a.hotel_id = %s AND a.room_type_id = %s",
        (hotel_id, room_type_id),
    ).fetchone()
    assert inventory == (5, 0, 0)


# ---------------------------------------------------------------------------
# Audit (plan section D6)
# ---------------------------------------------------------------------------


def test_audit_logs_cost_and_room_count_on_insert_and_update(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    start = _riyadh_today(db_conn) + timedelta(days=10)
    sign_in_as(admin_id)

    db_conn.execute(
        "SELECT * FROM admin_set_allotments(%s, %s, %s, %s, 4, 12000, false)",
        (hotel_id, room_type_id, start, start + timedelta(days=1)),
    )
    db_conn.execute(
        "SELECT * FROM admin_set_allotments(%s, %s, %s, %s, 6, 15000, false)",
        (hotel_id, room_type_id, start, start + timedelta(days=1)),
    )

    _reset_to_full_privilege(db_conn)
    allotment_row = db_conn.execute(
        "SELECT id FROM allotments WHERE hotel_id = %s AND room_type_id = %s",
        (hotel_id, room_type_id),
    ).fetchone()
    assert allotment_row is not None
    allotment_id = allotment_row[0]
    rows = db_conn.execute(
        "SELECT column_name, old_value, new_value, changed_by::text FROM audit_log "
        "WHERE table_name = 'allotments' AND row_id = %s ORDER BY id",
        (str(allotment_id),),
    ).fetchall()
    assert rows == [
        ("cost_per_night", None, 12000, admin_id),
        ("total_rooms", None, 4, admin_id),
        ("cost_per_night", 12000, 15000, admin_id),
        ("total_rooms", 4, 6, admin_id),
    ]


def test_room_count_audit_row_is_visible_without_cost_visibility(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    start = _riyadh_today(db_conn) + timedelta(days=10)
    sign_in_as(admin_id)
    db_conn.execute(
        "SELECT * FROM admin_set_allotments(%s, %s, %s, %s, 4, 12000, false)",
        (hotel_id, room_type_id, start, start + timedelta(days=1)),
    )
    _reset_to_full_privilege(db_conn)

    viewer_id = _seed_user(db_conn, role="admin", can_view_cost=False)
    sign_in_as(viewer_id)
    columns = db_conn.execute(
        "SELECT column_name FROM audit_log WHERE table_name = 'allotments' ORDER BY id"
    ).fetchall()
    assert columns == [
        ("total_rooms",)
    ]  # cost_per_night stays hidden, exactly as designed


# ---------------------------------------------------------------------------
# Concurrency (real connections, no mocks — CLAUDE.md's mandatory
# inventory test requirement, adapted to this write path).
# ---------------------------------------------------------------------------


def _connect_as_admin(dsn: str, admin_id: str) -> psycopg.Connection[Any]:
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute("SET SESSION AUTHORIZATION authenticated")
    conn.execute(
        sql.SQL("SET request.jwt.claim.sub = {}").format(sql.Literal(admin_id))
    )
    return conn


def test_reducing_rooms_races_a_hold_on_the_same_night_without_ever_overselling(
    db_conn: psycopg.Connection[Any], test_database_url: str
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    night = _riyadh_today(db_conn) + timedelta(days=20)
    seed_allotment_nights(
        db_conn, hotel_id, room_type_id, night, nights=1, total_rooms=5
    )
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)

    barrier = threading.Barrier(2)
    outcomes: dict[str, Any] = {}

    def reduce_to_zero() -> None:
        with _connect_as_admin(test_database_url, admin_id) as conn:
            barrier.wait()
            try:
                conn.execute(
                    "SELECT * FROM "
                    "admin_set_allotments(%s, %s, %s, %s, 0, 10000, false)",
                    (hotel_id, room_type_id, night, night + timedelta(days=1)),
                )
                outcomes["reduce"] = "reduced"
            except Exception as exc:
                outcomes["reduce"] = exc

    def take_all_rooms() -> None:
        with psycopg.connect(test_database_url) as conn:
            barrier.wait()
            try:
                outcomes["hold"] = create_hold(
                    conn,
                    hotel_id,
                    room_type_id,
                    night,
                    night + timedelta(days=1),
                    5,
                    datetime.now(UTC),
                    idempotency_key="races-a-reduction",
                )
            except Exception as exc:
                outcomes["hold"] = exc

    threads = [
        threading.Thread(target=reduce_to_zero),
        threading.Thread(target=take_all_rooms),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=_THREAD_JOIN_TIMEOUT_S)
    assert not any(thread.is_alive() for thread in threads), f"deadlock: {outcomes}"

    inventory_row = db_conn.execute(
        "SELECT total, reserved, held FROM room_night_inventory rni "
        "JOIN allotments a ON a.id = rni.allotment_id "
        "WHERE a.hotel_id = %s AND a.room_type_id = %s",
        (hotel_id, room_type_id),
    ).fetchone()
    assert inventory_row is not None
    total, reserved, held = inventory_row
    assert (
        reserved + held <= total
    )  # inventory_never_oversold, proven live, not just declared

    reduced = outcomes["reduce"] == "reduced"
    held_all = isinstance(outcomes["hold"], int)
    assert reduced != held_all, f"expected exactly one side to win the race: {outcomes}"
    if reduced:
        assert isinstance(outcomes["hold"], InsufficientInventoryError), outcomes
    else:
        assert isinstance(outcomes["reduce"], psycopg.errors.RaiseException), outcomes
        assert "cannot reduce rooms" in str(outcomes["reduce"])


def test_two_concurrent_entries_over_the_same_new_range_create_no_duplicates(
    db_conn: psycopg.Connection[Any], test_database_url: str
) -> None:
    """Two admins entering the exact same brand-new range at once: the
    allotments_hotel_id_room_type_id_stay_date_key UNIQUE constraint is
    still the real authority (CLAUDE.md rule 3) — the loser sees a raw
    UniqueViolation rather than a friendly message, a known rough edge
    worth the owner's attention, not a silent gap. What must never happen
    is a duplicate row or a deadlock, both proven here."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    start = _riyadh_today(db_conn) + timedelta(days=30)

    barrier = threading.Barrier(2)
    outcomes: dict[int, Any] = {}

    def enter(thread_id: int) -> None:
        with _connect_as_admin(test_database_url, admin_id) as conn:
            barrier.wait()
            try:
                rows = conn.execute(
                    "SELECT * FROM "
                    "admin_set_allotments(%s, %s, %s, %s, 3, 10000, false)",
                    (hotel_id, room_type_id, start, start + timedelta(days=3)),
                ).fetchall()
                outcomes[thread_id] = len(rows)
            except Exception as exc:
                outcomes[thread_id] = exc

    threads = [threading.Thread(target=enter, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=_THREAD_JOIN_TIMEOUT_S)
    assert not any(thread.is_alive() for thread in threads), f"deadlock: {outcomes}"

    count = db_conn.execute(
        "SELECT count(*) FROM allotments WHERE hotel_id = %s AND room_type_id = %s "
        "AND stay_date >= %s AND stay_date < %s",
        (hotel_id, room_type_id, start, start + timedelta(days=3)),
    ).fetchone()
    assert count == (3,)  # never duplicated, whichever way the race resolved

    successes = [v for v in outcomes.values() if isinstance(v, int)]
    failures = [v for v in outcomes.values() if isinstance(v, Exception)]
    assert len(successes) >= 1, f"at least one concurrent call must succeed: {outcomes}"
    for exc in failures:
        assert isinstance(exc, psycopg.errors.UniqueViolation), (
            f"a losing concurrent create must fail on the unique constraint, "
            f"not some other error: {exc!r}"
        )
