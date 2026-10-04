"""Seeding helpers for the price-import tables (migrations 0037 and 0038).

Not a test module itself (no test_ prefix), so pytest does not collect it.
Every value here is synthetic: no client hotel, price or date.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import psycopg

from tests.integration._seed import returning_id

# A well-formed sha256 for rate_import_batches.validated_fingerprint: the
# schema checks its shape only, the real one is computed by the validation.
RATE_IMPORT_FINGERPRINT = "a" * 64


def seed_app_user(
    conn: psycopg.Connection[Any],
    *,
    role: str,
    can_view_cost: bool = False,
    is_active: bool = True,
) -> str:
    """An auth.users identity with an app_users row. Unlike seed_actor it
    does not make the user the acting one: see act_as."""
    row = conn.execute("INSERT INTO auth.users DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    user_id = str(row[0])
    conn.execute(
        "INSERT INTO app_users (id, full_name, app_role, can_view_cost, is_active) "
        "VALUES (%s, 'Test User', %s, %s, %s)",
        (user_id, role, can_view_cost, is_active),
    )
    return user_id


def act_as(conn: psycopg.Connection[Any], user_id: str) -> None:
    """Names the staff member a backend connection acts for (app.actor_id),
    session-wide for the reason seed_actor gives."""
    conn.execute("SELECT set_config('app.actor_id', %s, false)", (user_id,))


def seed_rate_import_batch(conn: psycopg.Connection[Any], hotel_id: int) -> int:
    """A draft batch, created by whoever the connection is acting as."""
    return returning_id(
        conn,
        "INSERT INTO rate_import_batches (hotel_id, price_type) "
        "VALUES (%s, 'sell') RETURNING id",
        (hotel_id,),
    )


def seed_rate_import_row(
    conn: psycopg.Connection[Any],
    batch_id: int,
    hotel_id: int,
    room_type_id: int,
    *,
    period_start: date = date(2027, 1, 1),
    period_end: date = date(2027, 1, 10),
    weekday_price_halalas: int | None = 40_000,
    weekend_price_halalas: int | None = 50_000,
    is_closed: bool = False,
) -> int:
    return returning_id(
        conn,
        "INSERT INTO rate_import_rows (batch_id, hotel_id, room_type_id, "
        "period_start, period_end, weekday_price_halalas, weekend_price_halalas, "
        "is_closed) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            batch_id,
            hotel_id,
            room_type_id,
            period_start,
            period_end,
            weekday_price_halalas,
            weekend_price_halalas,
            is_closed,
        ),
    )


def seed_rate_import_night(
    conn: psycopg.Connection[Any],
    batch_id: int,
    hotel_id: int,
    room_type_id: int,
    *,
    stay_date: date = date(2027, 1, 1),
    sell_price_halalas: int = 40_000,
) -> None:
    conn.execute(
        "INSERT INTO rate_import_nights (batch_id, hotel_id, room_type_id, "
        "stay_date, sell_price_halalas) VALUES (%s, %s, %s, %s, %s)",
        (batch_id, hotel_id, room_type_id, stay_date, sell_price_halalas),
    )


def set_rate_import_status(
    conn: psycopg.Connection[Any], batch_id: int, status: str
) -> None:
    conn.execute(
        "UPDATE rate_import_batches SET status = %s WHERE id = %s", (status, batch_id)
    )


def validate_rate_import_batch(conn: psycopg.Connection[Any], batch_id: int) -> None:
    """Settles both review choices, then moves the draft to validated the
    way the validation will: status and fingerprint in one statement."""
    conn.execute(
        "UPDATE rate_import_batches SET period_end_inclusive = true, "
        "period_years_confirmed = true WHERE id = %s",
        (batch_id,),
    )
    conn.execute(
        "UPDATE rate_import_batches SET status = 'validated', "
        "validated_fingerprint = %s WHERE id = %s",
        (RATE_IMPORT_FINGERPRINT, batch_id),
    )


# The statuses a batch passes through, in order, to reach each status.
_RATE_IMPORT_STATUS_PATHS = {
    "draft": (),
    "rejected": ("rejected",),
    "validated": ("validated",),
    "approved": ("validated", "approved"),
    "disabled": ("validated", "approved", "disabled"),
}


def advance_rate_import_batch(
    conn: psycopg.Connection[Any], batch_id: int, status: str
) -> None:
    """Takes a draft batch to `status` through the legal transitions. The
    acting user must be allowed to approve when the path passes through
    approved."""
    for step in _RATE_IMPORT_STATUS_PATHS[status]:
        if step == "validated":
            validate_rate_import_batch(conn, batch_id)
        else:
            set_rate_import_status(conn, batch_id, step)


def seed_rate_import_batch_in(
    conn: psycopg.Connection[Any], hotel_id: int, status: str
) -> int:
    """A new batch taken to `status`: see advance_rate_import_batch."""
    batch_id = seed_rate_import_batch(conn, hotel_id)
    advance_rate_import_batch(conn, batch_id, status)
    return batch_id
