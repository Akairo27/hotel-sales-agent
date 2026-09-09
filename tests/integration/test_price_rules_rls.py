"""Verifies migration 0018's write path for price_rules, migration 0019's
masking view, and migration 0020's disable flag against a real Postgres
instance — see CLAUDE.md rule 3: the DB constraint is the source of truth,
not application discipline.

admin_upsert_price_rule is the only supported way to create or edit a
rule's three financial/demand fields; admin_set_price_rule_active is the
only supported way to toggle is_active. Both rely on AFTER INSERT/UPDATE
triggers that reject any write reaching the row without app.actor_id set
first — bypassing either wrapper fails loudly instead of silently skipping
the audit trail, same shape as migration 0016's allotments write path.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from tests.integration._seed import flat_demand_curve, flat_min_profit

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


def _upsert(
    conn: psycopg.Connection[Any],
    *,
    scope: str,
    scope_id: int | None = None,
    target_margin_bps: int = 1000,
    min_profit_by_lead_time: dict[str, Any] | None = None,
    demand_curve: dict[str, Any] | None = None,
) -> int:
    row = conn.execute(
        "SELECT admin_upsert_price_rule(%s, %s, %s, %s::jsonb, %s::jsonb)",
        (
            scope,
            scope_id,
            target_margin_bps,
            psycopg.types.json.Json(min_profit_by_lead_time or flat_min_profit(500)),
            psycopg.types.json.Json(demand_curve or flat_demand_curve()),
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


# --- admin_upsert_price_rule: create, update, audit -------------------


def test_admin_with_can_view_cost_can_create_the_global_rule(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)

    rule_id = _upsert(db_conn, scope="global", target_margin_bps=1000)

    row = db_conn.execute(
        "SELECT target_margin_bps, demand_curve FROM price_rules_for_dashboard "
        "WHERE id = %s",
        (rule_id,),
    ).fetchone()
    assert row == (1000, flat_demand_curve())


def test_admin_with_can_view_cost_can_update_an_existing_rule(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)
    rule_id = _upsert(db_conn, scope="global", target_margin_bps=1000)

    second_id = _upsert(db_conn, scope="global", target_margin_bps=1500)

    assert second_id == rule_id  # same row, updated in place -- not a new one
    row = db_conn.execute(
        "SELECT target_margin_bps FROM price_rules_for_dashboard WHERE id = %s",
        (rule_id,),
    ).fetchone()
    assert row == (1500,)


def test_admin_upsert_creating_a_rule_writes_four_audit_rows(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """The INSERT path: all three financial/demand fields are new, plus
    is_active's own DEFAULT TRUE (migration 0020's audit trigger treats
    every column it tracks as changed-from-nothing on INSERT, is_active
    included) -- four rows total, all with old_value NULL."""
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)

    rule_id = _upsert(db_conn, scope="global", target_margin_bps=1000)

    rows = db_conn.execute(
        "SELECT column_name, old_value, new_value, changed_by::text FROM audit_log "
        "WHERE table_name = 'price_rules' AND row_id = %s ORDER BY column_name",
        (str(rule_id),),
    ).fetchall()
    assert rows == [
        ("demand_curve", None, flat_demand_curve(), admin_id),
        ("is_active", None, True, admin_id),
        ("min_profit_by_lead_time", None, flat_min_profit(500), admin_id),
        ("target_margin_bps", None, 1000, admin_id),
    ]


def test_admin_upsert_updating_only_margin_writes_one_audit_row(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)
    rule_id = _upsert(db_conn, scope="global", target_margin_bps=1000)

    _upsert(db_conn, scope="global", target_margin_bps=2000)

    rows = db_conn.execute(
        "SELECT column_name, old_value, new_value FROM audit_log "
        "WHERE table_name = 'price_rules' AND row_id = %s "
        "AND column_name = 'target_margin_bps' ORDER BY id",
        (str(rule_id),),
    ).fetchall()
    # One row from the create (NULL -> 1000), one from this update (1000 -> 2000).
    assert rows == [
        ("target_margin_bps", None, 1000),
        ("target_margin_bps", 1000, 2000),
    ]


# --- write access: admin + can_view_cost only --------------------------


def test_admin_without_can_view_cost_cannot_create_a_rule(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=False)
    sign_in_as(admin_id)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _upsert(db_conn, scope="global", target_margin_bps=1000)


def test_sales_cannot_create_a_rule(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    sales_id = _seed_user(db_conn, role="sales", can_view_cost=True)
    sign_in_as(sales_id)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _upsert(db_conn, scope="global", target_margin_bps=1000)


def test_admin_without_can_view_cost_cannot_update_an_existing_rule(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """A denied UPDATE surfaces as admin_upsert_price_rule's own explicit
    permission error (via its EXISTS re-check), not a confusing
    unique-violation from falling through to the INSERT branch."""
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)
    _upsert(db_conn, scope="global", target_margin_bps=1000)

    db_conn.execute("RESET SESSION AUTHORIZATION")
    db_conn.execute("RESET request.jwt.claim.sub")
    blind_admin_id = _seed_user(db_conn, role="admin", can_view_cost=False)
    sign_in_as(blind_admin_id)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _upsert(db_conn, scope="global", target_margin_bps=2000)


def test_bypassing_the_wrapper_is_rejected_not_silently_unlogged(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)
    rule_id = _upsert(db_conn, scope="global", target_margin_bps=1000)

    with pytest.raises(psycopg.errors.RaiseException, match=r"app\.actor_id"):
        db_conn.execute(
            "UPDATE price_rules SET target_margin_bps = 9999 WHERE id = %s",
            (rule_id,),
        )


def test_authenticated_has_no_delete_grant(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """No DELETE grant at all, matching hotels/room_types/seasons -- the
    reversible way out of a wrong-scope rule is is_active (migration
    0020), not deletion."""
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)
    rule_id = _upsert(db_conn, scope="global", target_margin_bps=1000)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute("DELETE FROM price_rules WHERE id = %s", (rule_id,))


# --- masking view: price_rules_for_dashboard ----------------------------


def test_masking_view_hides_margin_and_min_profit_without_cost_visibility(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)
    rule_id = _upsert(db_conn, scope="global", target_margin_bps=1000)

    db_conn.execute("RESET SESSION AUTHORIZATION")
    db_conn.execute("RESET request.jwt.claim.sub")
    sales_id = _seed_user(db_conn, role="sales", can_view_cost=False)
    sign_in_as(sales_id)

    row = db_conn.execute(
        "SELECT target_margin_bps, min_profit_by_lead_time, demand_curve, is_active "
        "FROM price_rules_for_dashboard WHERE id = %s",
        (rule_id,),
    ).fetchone()
    # demand_curve and is_active carry no cost signal and stay visible to
    # every active app_users row, same posture as migration 0018's design.
    assert row == (None, None, flat_demand_curve(), True)


def test_masking_view_shows_everything_with_cost_visibility(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)
    rule_id = _upsert(db_conn, scope="global", target_margin_bps=1000)

    row = db_conn.execute(
        "SELECT target_margin_bps, min_profit_by_lead_time "
        "FROM price_rules_for_dashboard WHERE id = %s",
        (rule_id,),
    ).fetchone()
    assert row == (1000, flat_min_profit(500))


def test_anon_cannot_select_price_rules(db_conn: psycopg.Connection[Any]) -> None:
    """anon has real Supabase-default schema USAGE on public (see
    tests/supabase_default_acl_baseline.sql), so Postgres resolves the
    view name fine. Unlike the base tables in this schema,
    price_rules_for_dashboard (created by migration 0018, replaced by
    0020) carries no per-object REVOKE of its own — it relies entirely on
    migration 0012's ALTER DEFAULT PRIVILEGES lockdown, which denies anon
    by default and is never re-granted back to it (only authenticated
    gets GRANT SELECT). Either way the result is InsufficientPrivilege,
    not UndefinedTable."""
    db_conn.execute("SET SESSION AUTHORIZATION anon")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute("SELECT * FROM price_rules_for_dashboard").fetchall()
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


# --- admin_set_price_rule_active: migration 0020 ------------------------


def test_admin_set_price_rule_active_toggles_and_is_reversible(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)
    hotel_row = db_conn.execute(
        "INSERT INTO hotels (hotel_name) VALUES ('Test Hotel') RETURNING id"
    ).fetchone()
    assert hotel_row is not None
    rule_id = _upsert(db_conn, scope="hotel", scope_id=int(hotel_row[0]))

    db_conn.execute("SELECT admin_set_price_rule_active(%s, false)", (rule_id,))
    row = db_conn.execute(
        "SELECT is_active FROM price_rules_for_dashboard WHERE id = %s", (rule_id,)
    ).fetchone()
    assert row == (False,)

    db_conn.execute("SELECT admin_set_price_rule_active(%s, true)", (rule_id,))
    row = db_conn.execute(
        "SELECT is_active FROM price_rules_for_dashboard WHERE id = %s", (rule_id,)
    ).fetchone()
    assert row == (True,)

    rows = db_conn.execute(
        "SELECT old_value, new_value FROM audit_log "
        "WHERE table_name = 'price_rules' AND row_id = %s "
        "AND column_name = 'is_active' ORDER BY id",
        (str(rule_id),),
    ).fetchall()
    # Row 1 is the creation itself: is_active's DEFAULT TRUE is logged as a
    # change from NULL (migration 0018's audit trigger treats an INSERT's
    # every set column as a change from nothing) -- then the two toggles.
    assert rows == [(None, True), (True, False), (False, True)]


def test_admin_set_price_rule_active_cannot_deactivate_the_global_rule(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """price_rules_global_always_active (migration 0020): disabling the
    base of the inheritance chain is made unrepresentable, not just
    discouraged -- it would break every quote in the system."""
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)
    rule_id = _upsert(db_conn, scope="global", target_margin_bps=1000)

    with pytest.raises(
        psycopg.errors.CheckViolation, match="price_rules_global_always_active"
    ):
        db_conn.execute("SELECT admin_set_price_rule_active(%s, false)", (rule_id,))


def test_admin_without_can_view_cost_cannot_toggle_active(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)
    hotel_row = db_conn.execute(
        "INSERT INTO hotels (hotel_name) VALUES ('Test Hotel') RETURNING id"
    ).fetchone()
    assert hotel_row is not None
    rule_id = _upsert(db_conn, scope="hotel", scope_id=int(hotel_row[0]))

    db_conn.execute("RESET SESSION AUTHORIZATION")
    db_conn.execute("RESET request.jwt.claim.sub")
    blind_admin_id = _seed_user(db_conn, role="admin", can_view_cost=False)
    sign_in_as(blind_admin_id)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute("SELECT admin_set_price_rule_active(%s, false)", (rule_id,))
