"""Seeding helpers shared by services/inventory integration tests.

Not a test module itself (no test_ prefix), so pytest does not collect it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.types.json import Json


def returning_id(
    conn: psycopg.Connection[Any], query: str, params: tuple[Any, ...] = ()
) -> int:
    """Runs an INSERT ... RETURNING id and returns that id."""
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return int(row[0])


def seed_hotel_and_room_type(conn: psycopg.Connection[Any]) -> tuple[int, int]:
    hotel_id = returning_id(
        conn, "INSERT INTO hotels (hotel_name) VALUES ('Test Hotel') RETURNING id"
    )
    room_type_id = returning_id(
        conn,
        "INSERT INTO room_types (hotel_id, room_type_name) VALUES (%s, 'Standard') "
        "RETURNING id",
        (hotel_id,),
    )
    return hotel_id, room_type_id


def seed_season(
    conn: psycopg.Connection[Any],
    *,
    season_name: str,
    calendar_type: str,
    start_month: int,
    start_day: int,
    end_month: int,
    end_day: int,
    priority: int = 0,
    is_default: bool = False,
) -> int:
    return returning_id(
        conn,
        "INSERT INTO seasons (season_name, calendar_type, start_month, start_day, "
        "end_month, end_day, priority, is_default) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            season_name,
            calendar_type,
            start_month,
            start_day,
            end_month,
            end_day,
            priority,
            is_default,
        ),
    )


def seed_allotment_night(
    conn: psycopg.Connection[Any],
    hotel_id: int,
    room_type_id: int,
    stay_date: date,
    *,
    total_rooms: int,
    reserved: int = 0,
    held: int = 0,
    cost_per_night: int = 10_000,
) -> int:
    """Creates one allotment + room_night_inventory row for a single
    night, with full control over reserved/held — unlike
    seed_allotment_nights (below), which seeds several consecutive nights
    but always starts them at reserved=held=0. Pricing tests need
    per-night occupancy control that inventory tests never have.
    """
    allotment_id = returning_id(
        conn,
        "INSERT INTO allotments (hotel_id, room_type_id, stay_date, total_rooms, "
        "cost_per_night) VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (hotel_id, room_type_id, stay_date, total_rooms, cost_per_night),
    )
    conn.execute(
        "INSERT INTO room_night_inventory (allotment_id, stay_date, total, reserved, "
        "held) VALUES (%s, %s, %s, %s, %s)",
        (allotment_id, stay_date, total_rooms, reserved, held),
    )
    return allotment_id


def seed_actor(
    conn: psycopg.Connection[Any], *, role: str = "admin", can_view_cost: bool = True
) -> str:
    """Seeds a real auth.users + app_users row and points app.actor_id at
    it for the rest of this connection's session.

    The price_rules audit trigger (migration 0018) requires app.actor_id
    to be set on every INSERT as well as every UPDATE, and rejects a bare
    uuid that doesn't back a real row — audit_log.changed_by is a real
    foreign key to app_users, not just a column typed uuid. set_config's
    third argument is False (session-scoped), not True (SET LOCAL): db_conn
    is autocommit (see tests/conftest.py), so under autocommit each
    execute() is its own transaction and a SET LOCAL value would already
    have expired before the next statement — the very reset-to-empty-string
    behavior current_actor_id() (migration 0016) exists to survive, not
    something to lean on here.
    """
    row = conn.execute("INSERT INTO auth.users DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    actor_uuid = str(row[0])
    conn.execute(
        "INSERT INTO app_users (id, full_name, app_role, can_view_cost) "
        "VALUES (%s, 'Seeded Actor', %s, %s)",
        (actor_uuid, role, can_view_cost),
    )
    conn.execute("SELECT set_config('app.actor_id', %s, false)", (actor_uuid,))
    return actor_uuid


def seed_price_rule(
    conn: psycopg.Connection[Any],
    *,
    scope: str,
    scope_id: int | None = None,
    target_margin_bps: int | None = None,
    min_profit_by_lead_time: dict[str, Any] | None = None,
    demand_curve: dict[str, Any] | None = None,
    is_active: bool = True,
) -> int:
    """Inserts a price_rules row. Any field left None stays NULL — i.e.
    "this scope doesn't override this field, fall through" — per the
    field-by-field inheritance design.

    Seeds a fresh actor (see seed_actor) before every call: the audit
    trigger (migration 0018) rejects an INSERT with no app.actor_id set,
    and this helper's callers are testing price rule resolution, not
    audit attribution, so a throwaway actor is created each time rather
    than asking every call site to manage one.
    """
    seed_actor(conn)
    return returning_id(
        conn,
        "INSERT INTO price_rules (scope, scope_id, target_margin_bps, "
        "min_profit_by_lead_time, demand_curve, is_active) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (
            scope,
            scope_id,
            target_margin_bps,
            Json(min_profit_by_lead_time)
            if min_profit_by_lead_time is not None
            else None,
            Json(demand_curve) if demand_curve is not None else None,
            is_active,
        ),
    )


def flat_min_profit(halalas: int) -> dict[str, Any]:
    """A minimal valid min_profit_by_lead_time: one band, flat over the
    whole lead-time domain, for tests that need *a* valid value and
    aren't specifically exercising lead-time-band behavior."""
    return {
        "bands": [
            {"min_lead_days": 0, "max_lead_days": None, "min_profit_halalas": halalas}
        ]
    }


def flat_demand_curve(multiplier_bps: int = 10_000) -> dict[str, Any]:
    """A minimal valid demand_curve: one flat band per axis (default
    1.00x, i.e. no demand adjustment), for tests that need *a* valid
    value and aren't specifically exercising demand-curve behavior."""
    return {
        "occupancy_bands": [{"min": 0, "max": 1, "multiplier_bps": multiplier_bps}],
        "lead_time_bands": [
            {
                "min_lead_days": 0,
                "max_lead_days": None,
                "multiplier_bps": multiplier_bps,
            }
        ],
    }


def seed_price_override(
    conn: psycopg.Connection[Any],
    hotel_id: int,
    room_type_id: int,
    stay_date: date,
    *,
    ask_price_override: int = 50_000,
    min_allowed_override: int = 40_000,
    expires_at: datetime | None = None,
) -> int:
    """Inserts a price_overrides row directly, bypassing
    admin_upsert_price_overrides -- for tests that need a pre-existing row
    to check against (RLS reads, audit-log checks, upsert-overwrite
    behavior) without exercising the RPC itself.

    Seeds a fresh actor first (see seed_actor): the audit trigger
    (migration 0021) rejects an INSERT with no app.actor_id set, same as
    price_rules'. expires_at defaults 30 days out so callers that don't
    care about expiry get an unambiguously active row.
    """
    seed_actor(conn)
    if expires_at is None:
        expires_at = datetime.now(UTC) + timedelta(days=30)
    return returning_id(
        conn,
        "INSERT INTO price_overrides (hotel_id, room_type_id, stay_date, "
        "ask_price_override, min_allowed_override, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (
            hotel_id,
            room_type_id,
            stay_date,
            ask_price_override,
            min_allowed_override,
            expires_at,
        ),
    )


def seed_allotment_nights(
    conn: psycopg.Connection[Any],
    hotel_id: int,
    room_type_id: int,
    check_in: date,
    nights: int,
    total_rooms: int,
    cost_per_night: int = 10_000,
) -> None:
    """Creates one allotment and one room_night_inventory row per night,
    each starting with `total_rooms` capacity and zero reserved/held."""
    for offset in range(nights):
        night = check_in + timedelta(days=offset)
        allotment_id = returning_id(
            conn,
            "INSERT INTO allotments (hotel_id, room_type_id, stay_date, total_rooms, "
            "cost_per_night) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (hotel_id, room_type_id, night, total_rooms, cost_per_night),
        )
        conn.execute(
            "INSERT INTO room_night_inventory (allotment_id, stay_date, total) "
            "VALUES (%s, %s, %s)",
            (allotment_id, night, total_rooms),
        )


# One override-applied night, no computation-detail fields required — the
# minimal structurally valid quotes.nights shape. See migration
# 0009_quotes_nights_audit.sql.
_VALID_QUOTE_NIGHTS = (
    '[{"date": "2026-09-01", "season_id": 1, "ask": 20000, "min_allowed": 10000, '
    '"override_applied": true}]'
)


def seed_quote(
    conn: psycopg.Connection[Any],
    hotel_id: int,
    room_type_id: int,
    *,
    customer_phone: str | None = None,
    conversation_id: int | None = None,
    ask_price_total: int = 20_000,
    min_allowed_total: int = 10_000,
) -> int:
    return returning_id(
        conn,
        "INSERT INTO quotes (hotel_id, room_type_id, check_in, check_out, rooms, "
        "ask_price_total, min_allowed_total, nights, negotiation_open, "
        "customer_phone, conversation_id) "
        "VALUES (%s, %s, '2026-09-01', '2026-09-02', 1, %s, %s, %s::jsonb, "
        "true, %s, %s) RETURNING id",
        (
            hotel_id,
            room_type_id,
            ask_price_total,
            min_allowed_total,
            _VALID_QUOTE_NIGHTS,
            customer_phone,
            conversation_id,
        ),
    )


def seed_hold(
    conn: psycopg.Connection[Any],
    hotel_id: int,
    room_type_id: int,
    *,
    check_in: date = date(2026, 9, 1),
    check_out: date = date(2026, 9, 2),
    rooms: int = 1,
    expires_at: datetime | None = None,
) -> int:
    """Inserts a holds row directly, bypassing create_hold — for tests that
    need a pre-existing hold (e.g. a bookings row referencing it) without
    exercising services/inventory's own transaction/locking logic. A random
    idempotency_key per call satisfies the UNIQUE constraint (migration
    0005) without callers having to invent one."""
    if expires_at is None:
        expires_at = datetime.now(UTC) + timedelta(hours=12)
    return returning_id(
        conn,
        "INSERT INTO holds (hotel_id, room_type_id, check_in, check_out, rooms, "
        "expires_at, requires_full_payment, idempotency_key) "
        "VALUES (%s, %s, %s, %s, %s, %s, false, %s) RETURNING id",
        (
            hotel_id,
            room_type_id,
            check_in,
            check_out,
            rooms,
            expires_at,
            f"test-hold-{uuid4()}",
        ),
    )


def seed_conversation(
    conn: psycopg.Connection[Any], *, customer_phone: str = "+966500000001"
) -> int:
    return returning_id(
        conn,
        "INSERT INTO conversations (customer_phone) VALUES (%s) RETURNING id",
        (customer_phone,),
    )
