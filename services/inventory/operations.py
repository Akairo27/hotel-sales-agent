"""Inventory operations: availability, temporary holds, confirmation, and
release — see ARCHITECTURE.md §6.

Every write happens inside a real transaction with SELECT ... FOR UPDATE.
The inventory_never_oversold constraint is what actually prevents an
oversell; every check here is advisory and exists only to fail fast with a
clear error before the database would reject the write. CLAUDE.md rule 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, cast

import psycopg

from services.inventory.errors import (
    AllotmentNotFoundError,
    DuplicateHoldRequestError,
    FullPaymentRequiredError,
    HoldAlreadyResolvedError,
    HoldExpiredError,
    HoldNotFoundError,
    InsufficientInventoryError,
    RoomNightCountMismatchError,
)
from services.inventory.hold_windows import hold_window_for

_RANGE_JOIN = (
    "FROM room_night_inventory rni "
    "JOIN allotments a ON a.id = rni.allotment_id "
    "WHERE a.hotel_id = %(hotel_id)s AND a.room_type_id = %(room_type_id)s "
    "AND rni.stay_date >= %(check_in)s AND rni.stay_date < %(check_out)s"
)


def _nights_count(check_in: date, check_out: date) -> int:
    if check_out <= check_in:
        raise ValueError("check_out must be after check_in")
    return (check_out - check_in).days


def _range_params(
    hotel_id: int, room_type_id: int, check_in: date, check_out: date
) -> dict[str, Any]:
    return {
        "hotel_id": hotel_id,
        "room_type_id": room_type_id,
        "check_in": check_in,
        "check_out": check_out,
    }


@dataclass(frozen=True)
class StayAvailability:
    """Why a stay cannot be sold, night by night — two separate reasons a
    customer must be told apart: a night that is open for sale but lacks
    free rooms, and a night that has no inventory at all (not open for
    booking yet, never "fully booked"). Both lists are sorted and empty
    when every night can supply the rooms asked for. No room counts."""

    unavailable_nights: tuple[date, ...]
    nights_without_allotment: tuple[date, ...]

    @property
    def is_available(self) -> bool:
        return not self.unavailable_nights and not self.nights_without_allotment


def stay_availability(
    conn: psycopg.Connection[Any],
    hotel_id: int,
    room_type_id: int,
    check_in: date,
    check_out: date,
    rooms: int,
) -> StayAvailability:
    """Advisory-only and read-only: which nights of the stay cannot supply
    `rooms` rooms, split by reason (see StayAvailability). A night counts
    as without allotment when it has no room_night_inventory row, whether
    or not an allotments row exists. The database constraint, not this
    function, is what actually prevents an oversell — see create_hold.

    Raises:
        ValueError: check_out is not after check_in, or rooms is not
            positive.
    """
    nights = _nights_count(check_in, check_out)
    if rooms <= 0:
        raise ValueError("rooms must be positive")

    rows = conn.execute(
        "SELECT rni.stay_date, rni.total, rni.reserved, rni.held " + _RANGE_JOIN,
        _range_params(hotel_id, room_type_id, check_in, check_out),
    ).fetchall()
    free_rooms = {
        stay_date: total - reserved - held for stay_date, total, reserved, held in rows
    }

    stay_dates = [check_in + timedelta(days=offset) for offset in range(nights)]
    return StayAvailability(
        unavailable_nights=tuple(
            night
            for night in stay_dates
            if night in free_rooms and free_rooms[night] < rooms
        ),
        nights_without_allotment=tuple(
            night for night in stay_dates if night not in free_rooms
        ),
    )


def check_availability(
    conn: psycopg.Connection[Any],
    hotel_id: int,
    room_type_id: int,
    check_in: date,
    check_out: date,
    rooms: int,
) -> bool:
    """Advisory-only: whether `rooms` rooms appear free for every night of
    the stay (stay_availability, with no night short or missing). The
    database constraint, not this function, is what actually prevents an
    oversell — see create_hold.
    """
    return stay_availability(
        conn, hotel_id, room_type_id, check_in, check_out, rooms
    ).is_available


def _lock_nights_for_update(
    conn: psycopg.Connection[Any],
    hotel_id: int,
    room_type_id: int,
    check_in: date,
    check_out: date,
    expected_nights: int,
) -> None:
    """Locks every room_night_inventory row for the stay so a concurrent
    hold on the same nights blocks until this transaction commits or rolls
    back. Raises if any night in the range has no allotment at all.
    """
    query = (
        "SELECT rni.stay_date "
        + _RANGE_JOIN
        + " ORDER BY rni.stay_date FOR UPDATE OF rni"
    )
    params = _range_params(hotel_id, room_type_id, check_in, check_out)
    locked = conn.execute(query, params).fetchall()
    if len(locked) != expected_nights:
        raise AllotmentNotFoundError(
            f"expected {expected_nights} nights of allotment for hotel {hotel_id}/"
            f"room type {room_type_id} between {check_in} and {check_out}, "
            f"found {len(locked)}"
        )


def _adjust_room_nights(
    conn: psycopg.Connection[Any],
    hotel_id: int,
    room_type_id: int,
    check_in: date,
    check_out: date,
    *,
    held_delta: int,
    reserved_delta: int,
    expected_nights: int,
) -> None:
    """Applies held/reserved deltas across every night of the stay. Deltas
    may be negative — callers hold the row locks that make this safe.

    Raises:
        RoomNightCountMismatchError: the UPDATE touched fewer rows than
            expected_nights. Without this check, a night missing from the
            range would silently absorb the delta on fewer rows than it was
            applied on the way in, leaving held or reserved permanently off
            by the untouched night's share — never surfaced, only
            accumulating. Callers are expected to have already locked
            exactly expected_nights rows via _lock_nights_for_update, so
            this is a backstop, not the primary defense.
    """
    params = _range_params(hotel_id, room_type_id, check_in, check_out)
    cursor = conn.execute(
        "UPDATE room_night_inventory rni "
        "SET held = held + %(held_delta)s, reserved = reserved + %(reserved_delta)s "
        "FROM allotments a "
        "WHERE a.id = rni.allotment_id "
        "AND a.hotel_id = %(hotel_id)s AND a.room_type_id = %(room_type_id)s "
        "AND rni.stay_date >= %(check_in)s AND rni.stay_date < %(check_out)s",
        {**params, "held_delta": held_delta, "reserved_delta": reserved_delta},
    )
    if cursor.rowcount != expected_nights:
        raise RoomNightCountMismatchError(
            f"expected to adjust {expected_nights} room-night rows for hotel "
            f"{hotel_id}/room type {room_type_id} between {check_in} and {check_out}, "
            f"but affected {cursor.rowcount}"
        )


def create_hold(
    conn: psycopg.Connection[Any],
    hotel_id: int,
    room_type_id: int,
    check_in: date,
    check_out: date,
    rooms: int,
    now: datetime,
    *,
    idempotency_key: str,
) -> int:
    """Creates a temporary hold across every night of the stay, in one
    transaction, per ARCHITECTURE.md §6.

    idempotency_key must be unique per request (e.g. derived from the
    WhatsApp message_id). Calling this twice with the same key returns the
    id of the hold created the first time, without incrementing held again
    — a retried request must never create a second hold.

    Raises:
        AllotmentNotFoundError: a night in the range has no allotment.
        InsufficientInventoryError: the database rejected the hold as an
            oversell — inventory_never_oversold is the actual authority.
        DuplicateHoldRequestError: a concurrent request with the same
            idempotency_key won the race to insert first.
    """
    expected_nights = _nights_count(check_in, check_out)
    if rooms <= 0:
        raise ValueError("rooms must be positive")

    with conn.transaction():
        existing = conn.execute(
            "SELECT id FROM holds WHERE idempotency_key = %s", (idempotency_key,)
        ).fetchone()
        if existing is not None:
            return int(existing[0])

        _lock_nights_for_update(
            conn, hotel_id, room_type_id, check_in, check_out, expected_nights
        )
        try:
            _adjust_room_nights(
                conn,
                hotel_id,
                room_type_id,
                check_in,
                check_out,
                held_delta=rooms,
                reserved_delta=0,
                expected_nights=expected_nights,
            )
        except psycopg.errors.CheckViolation as exc:
            raise InsufficientInventoryError(
                f"insufficient inventory for hotel {hotel_id}/room type {room_type_id} "
                f"between {check_in} and {check_out}"
            ) from exc

        window = hold_window_for(check_in, now)
        try:
            hold_row = conn.execute(
                "INSERT INTO holds (hotel_id, room_type_id, check_in, check_out, "
                "rooms, expires_at, requires_full_payment, idempotency_key) VALUES "
                "(%(hotel_id)s, %(room_type_id)s, %(check_in)s, %(check_out)s, "
                "%(rooms)s, %(expires_at)s, %(requires_full_payment)s, "
                "%(idempotency_key)s) RETURNING id",
                {
                    **_range_params(hotel_id, room_type_id, check_in, check_out),
                    "rooms": rooms,
                    "expires_at": now + window.duration,
                    "requires_full_payment": window.requires_full_payment,
                    "idempotency_key": idempotency_key,
                },
            ).fetchone()
        except psycopg.errors.UniqueViolation as exc:
            raise DuplicateHoldRequestError(
                f"a hold with idempotency_key {idempotency_key!r} was already "
                "created by a concurrent request"
            ) from exc
        # A successful single-row INSERT ... RETURNING always yields a row;
        # this reflects that guarantee rather than handling a real case.
        return int(cast(tuple[Any, ...], hold_row)[0])


def confirm_hold(
    conn: psycopg.Connection[Any],
    hold_id: int,
    now: datetime,
    *,
    payment_received_in_full: bool,
) -> None:
    """Moves a hold's rooms from held to reserved, after payment
    confirmation. Cash on arrival is never confirmed here — ARCHITECTURE.md
    §6 routes that to a human escalation instead.

    Raises:
        HoldNotFoundError: no hold with this id exists.
        HoldAlreadyResolvedError: the hold was already released or confirmed.
        HoldExpiredError: now is at or past the hold's expiry.
        FullPaymentRequiredError: the hold's window required full payment
            up front (see hold_windows.py) and payment_received_in_full is
            False — this must escalate to a human, not confirm silently.
    """
    with conn.transaction():
        row = conn.execute(
            "SELECT hotel_id, room_type_id, check_in, check_out, rooms, expires_at, "
            "released_at, confirmed_at, requires_full_payment "
            "FROM holds WHERE id = %s FOR UPDATE",
            (hold_id,),
        ).fetchone()
        if row is None:
            raise HoldNotFoundError(f"hold {hold_id} does not exist")
        (
            hotel_id,
            room_type_id,
            check_in,
            check_out,
            rooms,
            expires_at,
            released_at,
            confirmed_at,
            requires_full_payment,
        ) = row

        if released_at is not None or confirmed_at is not None:
            raise HoldAlreadyResolvedError(f"hold {hold_id} was already resolved")
        if now >= expires_at:
            raise HoldExpiredError(f"hold {hold_id} expired at {expires_at}")
        if requires_full_payment and not payment_received_in_full:
            raise FullPaymentRequiredError(
                f"hold {hold_id} requires full payment before confirmation "
                "— escalate to a human instead of confirming without it"
            )

        expected_nights = _nights_count(check_in, check_out)
        _lock_nights_for_update(
            conn, hotel_id, room_type_id, check_in, check_out, expected_nights
        )
        _adjust_room_nights(
            conn,
            hotel_id,
            room_type_id,
            check_in,
            check_out,
            held_delta=-rooms,
            reserved_delta=rooms,
            expected_nights=expected_nights,
        )
        conn.execute("UPDATE holds SET confirmed_at = %s WHERE id = %s", (now, hold_id))


def release_hold(conn: psycopg.Connection[Any], hold_id: int, now: datetime) -> None:
    """Releases a hold's rooms back to available inventory.

    Releases exactly once: the claiming UPDATE only matches an
    unresolved hold, so a second call — concurrent or sequential — finds no
    row to update and raises instead of decrementing held twice.
    """
    with conn.transaction():
        released = conn.execute(
            "UPDATE holds SET released_at = %s "
            "WHERE id = %s AND released_at IS NULL AND confirmed_at IS NULL "
            "RETURNING hotel_id, room_type_id, check_in, check_out, rooms",
            (now, hold_id),
        ).fetchone()

        if released is None:
            exists = conn.execute(
                "SELECT 1 FROM holds WHERE id = %s", (hold_id,)
            ).fetchone()
            if exists is None:
                raise HoldNotFoundError(f"hold {hold_id} does not exist")
            raise HoldAlreadyResolvedError(f"hold {hold_id} was already resolved")

        hotel_id, room_type_id, check_in, check_out, rooms = released
        expected_nights = _nights_count(check_in, check_out)
        _lock_nights_for_update(
            conn, hotel_id, room_type_id, check_in, check_out, expected_nights
        )
        _adjust_room_nights(
            conn,
            hotel_id,
            room_type_id,
            check_in,
            check_out,
            held_delta=-rooms,
            reserved_delta=0,
            expected_nights=expected_nights,
        )
