"""Verifies migration 0021's write path for price_overrides against a real
Postgres instance -- see CLAUDE.md rule 3: the DB constraint is the source
of truth, not application discipline.

admin_upsert_price_overrides is the only supported way to create or edit a
night's override, and the only way to end one early (by resubmitting with
an expires_at in the past -- see migration 0021's own comment on why there
is no separate is_active-style column here). It relies on AFTER INSERT/
UPDATE triggers that reject any write reaching the row without
app.actor_id set first, same shape as price_rules' write path
(test_price_rules_rls.py).

Two deliberate divergences from price_rules, both tested explicitly below:
write access is admin-only but NOT can_view_cost-gated, and read access is
open to any active app_users row with no masking view -- neither of this
table's three columns reverse-derives cost (migration 0021's own comment).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest

from tests.integration._seed import seed_hotel_and_room_type

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
    hotel_id: int,
    room_type_id: int,
    start_date: date,
    end_date: date,
    ask_price_override: int = 50_000,
    min_allowed_override: int = 40_000,
    expires_at: datetime | None = None,
) -> list[int]:
    if expires_at is None:
        expires_at = datetime.now(UTC) + timedelta(days=30)
    rows = conn.execute(
        "SELECT * FROM admin_upsert_price_overrides(%s, %s, %s, %s, %s, %s, %s)",
        (
            hotel_id,
            room_type_id,
            start_date,
            end_date,
            ask_price_override,
            min_allowed_override,
            expires_at,
        ),
    ).fetchall()
    return [int(r[0]) for r in rows]


# --- admin_upsert_price_overrides: create, overwrite, audit -------------


def test_admin_can_create_a_single_night_override(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)

    ids = _upsert(
        db_conn,
        hotel_id=hotel_id,
        room_type_id=room_type_id,
        start_date=date(2027, 1, 1),
        end_date=date(2027, 1, 1),
    )

    assert len(ids) == 1
    row = db_conn.execute(
        "SELECT stay_date, ask_price_override, min_allowed_override "
        "FROM price_overrides WHERE id = %s",
        (ids[0],),
    ).fetchone()
    assert row == (date(2027, 1, 1), 50_000, 40_000)


def test_admin_can_create_a_multi_night_range(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)

    ids = _upsert(
        db_conn,
        hotel_id=hotel_id,
        room_type_id=room_type_id,
        start_date=date(2027, 1, 1),
        end_date=date(2027, 1, 3),
    )

    assert len(ids) == 3
    nights = db_conn.execute(
        "SELECT stay_date FROM price_overrides ORDER BY stay_date"
    ).fetchall()
    assert [n[0] for n in nights] == [
        date(2027, 1, 1),
        date(2027, 1, 2),
        date(2027, 1, 3),
    ]


def test_overlapping_range_overwrites_existing_nights_and_creates_new_ones(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """Confirmed empirically before this migration shipped that ON CONFLICT
    DO UPDATE needs price_overrides_select_for_active_users to work at
    all (see migration 0021's own comment) -- this test is what keeps that
    finding true in CI, not just a one-off manual check."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)
    _upsert(
        db_conn,
        hotel_id=hotel_id,
        room_type_id=room_type_id,
        start_date=date(2027, 1, 1),
        end_date=date(2027, 1, 3),
        ask_price_override=50_000,
    )

    _upsert(
        db_conn,
        hotel_id=hotel_id,
        room_type_id=room_type_id,
        start_date=date(2027, 1, 2),
        end_date=date(2027, 1, 4),
        ask_price_override=55_000,
    )

    rows = db_conn.execute(
        "SELECT stay_date, ask_price_override FROM price_overrides ORDER BY stay_date"
    ).fetchall()
    assert rows == [
        (date(2027, 1, 1), 50_000),  # untouched by the second call
        (date(2027, 1, 2), 55_000),  # overwritten
        (date(2027, 1, 3), 55_000),  # overwritten
        (date(2027, 1, 4), 55_000),  # newly created
    ]


def test_admin_upsert_creating_a_night_writes_three_audit_rows(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)

    ids = _upsert(
        db_conn,
        hotel_id=hotel_id,
        room_type_id=room_type_id,
        start_date=date(2027, 1, 1),
        end_date=date(2027, 1, 1),
        ask_price_override=50_000,
        min_allowed_override=40_000,
    )

    rows = db_conn.execute(
        "SELECT column_name, old_value, new_value FROM audit_log "
        "WHERE table_name = 'price_overrides' AND row_id = %s ORDER BY column_name",
        (str(ids[0]),),
    ).fetchall()
    assert [r[0] for r in rows] == [
        "ask_price_override",
        "expires_at",
        "min_allowed_override",
    ]
    assert rows[0][1] is None and rows[0][2] == 50_000
    assert rows[2][1] is None and rows[2][2] == 40_000


def test_resubmitting_only_a_new_ask_price_writes_one_audit_row(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)
    expires_at = datetime.now(UTC) + timedelta(days=30)
    ids = _upsert(
        db_conn,
        hotel_id=hotel_id,
        room_type_id=room_type_id,
        start_date=date(2027, 1, 1),
        end_date=date(2027, 1, 1),
        ask_price_override=50_000,
        min_allowed_override=40_000,
        expires_at=expires_at,
    )

    _upsert(
        db_conn,
        hotel_id=hotel_id,
        room_type_id=room_type_id,
        start_date=date(2027, 1, 1),
        end_date=date(2027, 1, 1),
        ask_price_override=60_000,
        min_allowed_override=40_000,
        expires_at=expires_at,
    )

    rows = db_conn.execute(
        "SELECT old_value, new_value FROM audit_log "
        "WHERE table_name = 'price_overrides' AND row_id = %s "
        "AND column_name = 'ask_price_override' ORDER BY id",
        (str(ids[0]),),
    ).fetchall()
    assert rows == [(None, 50_000), (50_000, 60_000)]


def test_ending_early_sets_expires_at_and_is_audited(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """Ending an override is not a separate action -- it is resubmitting
    the same night with an expires_at in the past, per migration 0021's
    design (no is_active column). services/pricing/compute.py's
    _fetch_active_override already filters on this same column."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)
    future = datetime.now(UTC) + timedelta(days=30)
    ids = _upsert(
        db_conn,
        hotel_id=hotel_id,
        room_type_id=room_type_id,
        start_date=date(2027, 1, 1),
        end_date=date(2027, 1, 1),
        expires_at=future,
    )

    past = datetime.now(UTC) - timedelta(minutes=1)
    _upsert(
        db_conn,
        hotel_id=hotel_id,
        room_type_id=room_type_id,
        start_date=date(2027, 1, 1),
        end_date=date(2027, 1, 1),
        expires_at=past,
    )

    row = db_conn.execute(
        "SELECT expires_at FROM price_overrides WHERE id = %s", (ids[0],)
    ).fetchone()
    assert row is not None
    assert row[0] < datetime.now(UTC)
    audit_rows = db_conn.execute(
        "SELECT old_value, new_value FROM audit_log "
        "WHERE table_name = 'price_overrides' AND row_id = %s "
        "AND column_name = 'expires_at' ORDER BY id",
        (str(ids[0]),),
    ).fetchall()
    assert len(audit_rows) == 2  # creation, then the early end


# --- write access: admin only, deliberately NOT can_view_cost-gated -----


def test_admin_without_can_view_cost_can_still_create_an_override(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """The key divergence from price_rules (migration 0018): none of this
    table's columns reverse-derive cost, so writing here is not gated on
    can_view_cost -- only on being an admin."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=False)
    sign_in_as(admin_id)

    ids = _upsert(
        db_conn,
        hotel_id=hotel_id,
        room_type_id=room_type_id,
        start_date=date(2027, 1, 1),
        end_date=date(2027, 1, 1),
    )

    assert len(ids) == 1


def test_sales_cannot_create_an_override(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    sales_id = _seed_user(db_conn, role="sales", can_view_cost=True)
    sign_in_as(sales_id)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _upsert(
            db_conn,
            hotel_id=hotel_id,
            room_type_id=room_type_id,
            start_date=date(2027, 1, 1),
            end_date=date(2027, 1, 1),
        )


def test_bypassing_the_wrapper_is_rejected_not_silently_unlogged(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)
    ids = _upsert(
        db_conn,
        hotel_id=hotel_id,
        room_type_id=room_type_id,
        start_date=date(2027, 1, 1),
        end_date=date(2027, 1, 1),
    )

    with pytest.raises(psycopg.errors.RaiseException, match=r"app\.actor_id"):
        db_conn.execute(
            "UPDATE price_overrides SET ask_price_override = 99999 WHERE id = %s",
            (ids[0],),
        )


def test_authenticated_has_no_delete_grant(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)
    ids = _upsert(
        db_conn,
        hotel_id=hotel_id,
        room_type_id=room_type_id,
        start_date=date(2027, 1, 1),
        end_date=date(2027, 1, 1),
    )

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute("DELETE FROM price_overrides WHERE id = %s", (ids[0],))


# --- range validation: RAISE EXCEPTION backstop --------------------------


def test_start_date_after_end_date_rejected(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)

    with pytest.raises(psycopg.errors.RaiseException, match="start date"):
        _upsert(
            db_conn,
            hotel_id=hotel_id,
            room_type_id=room_type_id,
            start_date=date(2027, 1, 10),
            end_date=date(2027, 1, 1),
        )


def test_range_of_exactly_180_nights_accepted(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)
    start = date(2027, 1, 1)
    end = start + timedelta(days=179)  # inclusive range of 180 nights

    ids = _upsert(
        db_conn,
        hotel_id=hotel_id,
        room_type_id=room_type_id,
        start_date=start,
        end_date=end,
    )

    assert len(ids) == 180


def test_range_over_180_nights_rejected(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)
    start = date(2027, 1, 1)
    end = start + timedelta(days=180)  # inclusive range of 181 nights

    with pytest.raises(psycopg.errors.RaiseException, match="180 nights"):
        _upsert(
            db_conn,
            hotel_id=hotel_id,
            room_type_id=room_type_id,
            start_date=start,
            end_date=end,
        )


def test_room_type_must_belong_to_the_given_hotel(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    hotel_id, _room_type_id = seed_hotel_and_room_type(db_conn)
    _other_hotel_id, other_room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _upsert(
            db_conn,
            hotel_id=hotel_id,
            room_type_id=other_room_type_id,
            start_date=date(2027, 1, 1),
            end_date=date(2027, 1, 1),
        )


# --- read access: any active user, deliberately NOT masked --------------


def test_sales_without_can_view_cost_can_read_price_overrides(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """The other key divergence from price_rules: no masking view. Sales
    needs to see and act on these final prices the same as any other quote
    input (migration 0021's own comment)."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    admin_id = _seed_user(db_conn, role="admin", can_view_cost=True)
    sign_in_as(admin_id)
    _upsert(
        db_conn,
        hotel_id=hotel_id,
        room_type_id=room_type_id,
        start_date=date(2027, 1, 1),
        end_date=date(2027, 1, 1),
        ask_price_override=50_000,
    )

    db_conn.execute("RESET SESSION AUTHORIZATION")
    db_conn.execute("RESET request.jwt.claim.sub")
    sales_id = _seed_user(db_conn, role="sales", can_view_cost=False)
    sign_in_as(sales_id)

    rows = db_conn.execute(
        "SELECT ask_price_override FROM price_overrides WHERE hotel_id = %s",
        (hotel_id,),
    ).fetchall()
    assert rows == [(50_000,)]


def test_anon_cannot_select_price_overrides(db_conn: psycopg.Connection[Any]) -> None:
    """anon has real Supabase-default schema USAGE on public (see
    tests/supabase_default_acl_baseline.sql), so Postgres resolves the
    table name fine; migration 0007's own REVOKE ALL ON TABLE
    price_overrides FROM anon is what denies it, raising
    InsufficientPrivilege, not UndefinedTable."""
    db_conn.execute("SET SESSION AUTHORIZATION anon")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute("SELECT * FROM price_overrides").fetchall()
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")
