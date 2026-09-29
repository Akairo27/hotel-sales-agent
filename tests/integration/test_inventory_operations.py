"""Sequential (single-connection) tests for services/inventory/operations.py.

See test_inventory_concurrency.py for the mandatory real-concurrent-
transaction tests — those require two separate connections and are kept
apart from this file's simpler single-connection cases.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest

from services.inventory.errors import (
    AllotmentNotFoundError,
    FullPaymentRequiredError,
    HoldAlreadyResolvedError,
    HoldExpiredError,
    HoldNotFoundError,
    InsufficientInventoryError,
    RoomNightCountMismatchError,
)
from services.inventory.operations import (
    StayAvailability,
    _adjust_room_nights,
    check_availability,
    confirm_hold,
    create_hold,
    release_hold,
    stay_availability,
)
from tests.integration._seed import (
    seed_actor,
    seed_allotment_night,
    seed_allotment_nights,
    seed_hotel_and_room_type,
)

pytestmark = pytest.mark.usefixtures("db_conn")

_NOW = datetime(2026, 6, 1, tzinfo=UTC)
_CHECK_IN = date(2026, 7, 1)
_CHECK_OUT = date(2026, 7, 3)  # two nights


def _seed_two_night_stay(
    conn: psycopg.Connection[Any], total_rooms: int
) -> tuple[int, int]:
    hotel_id, room_type_id = seed_hotel_and_room_type(conn)
    seed_allotment_nights(
        conn, hotel_id, room_type_id, _CHECK_IN, nights=2, total_rooms=total_rooms
    )
    return hotel_id, room_type_id


def test_check_availability_true_when_capacity_exists(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = _seed_two_night_stay(db_conn, total_rooms=3)
    assert check_availability(db_conn, hotel_id, room_type_id, _CHECK_IN, _CHECK_OUT, 3)


def test_check_availability_false_when_insufficient_capacity(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = _seed_two_night_stay(db_conn, total_rooms=2)
    assert not check_availability(
        db_conn, hotel_id, room_type_id, _CHECK_IN, _CHECK_OUT, 3
    )


def test_check_availability_raises_when_check_out_not_after_check_in(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = _seed_two_night_stay(db_conn, total_rooms=1)
    with pytest.raises(ValueError, match="check_out must be after check_in"):
        check_availability(db_conn, hotel_id, room_type_id, _CHECK_OUT, _CHECK_IN, 1)


def test_check_availability_raises_when_rooms_not_positive(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = _seed_two_night_stay(db_conn, total_rooms=1)
    with pytest.raises(ValueError, match="rooms must be positive"):
        check_availability(db_conn, hotel_id, room_type_id, _CHECK_IN, _CHECK_OUT, 0)


def test_create_hold_raises_when_rooms_not_positive(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = _seed_two_night_stay(db_conn, total_rooms=1)
    with pytest.raises(ValueError, match="rooms must be positive"):
        create_hold(
            db_conn,
            hotel_id,
            room_type_id,
            _CHECK_IN,
            _CHECK_OUT,
            0,
            _NOW,
            idempotency_key="rooms-not-positive",
        )


def test_check_availability_false_when_night_missing_allotment(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    seed_allotment_nights(
        db_conn, hotel_id, room_type_id, _CHECK_IN, nights=1, total_rooms=5
    )
    # Only one of the two required nights has an allotment.
    assert not check_availability(
        db_conn, hotel_id, room_type_id, _CHECK_IN, _CHECK_OUT, 1
    )


_THREE_NIGHT_START = date(2026, 7, 10)


def _seed_nights(
    conn: psycopg.Connection[Any], nights: dict[int, tuple[int, int, int]]
) -> tuple[int, int]:
    """One allotment + inventory row per {day offset: (total, reserved,
    held)} from _THREE_NIGHT_START; offsets left out have no row at all."""
    hotel_id, room_type_id = seed_hotel_and_room_type(conn)
    for offset, (total, reserved, held) in nights.items():
        seed_allotment_night(
            conn,
            hotel_id,
            room_type_id,
            _THREE_NIGHT_START + timedelta(days=offset),
            total_rooms=total,
            reserved=reserved,
            held=held,
        )
    return hotel_id, room_type_id


def _three_night_availability(
    conn: psycopg.Connection[Any], hotel_id: int, room_type_id: int, rooms: int
) -> StayAvailability:
    return stay_availability(
        conn,
        hotel_id,
        room_type_id,
        _THREE_NIGHT_START,
        _THREE_NIGHT_START + timedelta(days=3),
        rooms,
    )


def _night(offset: int) -> date:
    return _THREE_NIGHT_START + timedelta(days=offset)


def test_stay_availability_is_empty_when_every_night_has_the_rooms(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Free rooms exactly equal to the request still count as available."""
    hotel_id, room_type_id = _seed_nights(
        db_conn, {0: (5, 0, 0), 1: (5, 3, 0), 2: (5, 1, 2)}
    )

    availability = _three_night_availability(db_conn, hotel_id, room_type_id, 2)

    assert availability == StayAvailability((), ())
    assert availability.is_available


@pytest.mark.parametrize(
    "night",
    [
        pytest.param((5, 5, 0), id="sold-out-by-reserved"),
        pytest.param((5, 0, 5), id="sold-out-by-held"),
        pytest.param((5, 4, 0), id="fewer-free-than-requested"),
        pytest.param((0, 0, 0), id="zero-total"),
    ],
)
def test_stay_availability_lists_a_short_night_as_unavailable(
    db_conn: psycopg.Connection[Any], night: tuple[int, int, int]
) -> None:
    hotel_id, room_type_id = _seed_nights(
        db_conn, {0: (5, 0, 0), 1: night, 2: (5, 0, 0)}
    )

    availability = _three_night_availability(db_conn, hotel_id, room_type_id, 2)

    assert availability == StayAvailability((_night(1),), ())
    assert not availability.is_available


def test_stay_availability_lists_a_night_with_no_inventory_row_separately(
    db_conn: psycopg.Connection[Any],
) -> None:
    """A night with no row at all is not open for booking -- never reported
    as unavailable (fully booked)."""
    hotel_id, room_type_id = _seed_nights(db_conn, {0: (5, 0, 0), 2: (5, 0, 0)})

    availability = _three_night_availability(db_conn, hotel_id, room_type_id, 1)

    assert availability == StayAvailability((), (_night(1),))
    assert not availability.is_available


def test_stay_availability_counts_an_allotment_without_inventory_as_missing(
    db_conn: psycopg.Connection[Any],
) -> None:
    """An allotments row with no room_night_inventory row (the trial
    database holds one -- migration 0029's comment) has nothing to sell."""
    hotel_id, room_type_id = _seed_nights(db_conn, {0: (5, 0, 0), 2: (5, 0, 0)})
    seed_actor(db_conn)
    db_conn.execute(
        "INSERT INTO allotments (hotel_id, room_type_id, stay_date, total_rooms, "
        "cost_per_night) VALUES (%s, %s, %s, 5, 10000)",
        (hotel_id, room_type_id, _night(1)),
    )

    availability = _three_night_availability(db_conn, hotel_id, room_type_id, 1)

    assert availability == StayAvailability((), (_night(1),))


def test_stay_availability_reports_short_and_missing_nights_together(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = _seed_nights(db_conn, {0: (5, 5, 0), 2: (5, 0, 0)})

    availability = _three_night_availability(db_conn, hotel_id, room_type_id, 1)

    assert availability == StayAvailability((_night(0),), (_night(1),))


def test_stay_availability_raises_when_rooms_not_positive(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = _seed_nights(db_conn, {0: (5, 0, 0)})
    with pytest.raises(ValueError, match="rooms must be positive"):
        _three_night_availability(db_conn, hotel_id, room_type_id, 0)


def test_create_hold_increments_held_and_returns_id(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = _seed_two_night_stay(db_conn, total_rooms=3)

    hold_id = create_hold(
        db_conn,
        hotel_id,
        room_type_id,
        _CHECK_IN,
        _CHECK_OUT,
        2,
        _NOW,
        idempotency_key="increments-held",
    )

    assert isinstance(hold_id, int)
    rows = db_conn.execute(
        "SELECT held, reserved FROM room_night_inventory rni "
        "JOIN allotments a ON a.id = rni.allotment_id "
        "WHERE a.hotel_id = %s AND a.room_type_id = %s",
        (hotel_id, room_type_id),
    ).fetchall()
    assert rows == [(2, 0), (2, 0)]


def test_create_hold_raises_when_night_missing_allotment(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    seed_allotment_nights(
        db_conn, hotel_id, room_type_id, _CHECK_IN, nights=1, total_rooms=5
    )

    with pytest.raises(AllotmentNotFoundError):
        create_hold(
            db_conn,
            hotel_id,
            room_type_id,
            _CHECK_IN,
            _CHECK_OUT,
            1,
            _NOW,
            idempotency_key="missing-allotment",
        )


def test_create_hold_impossible_to_exceed_total(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = _seed_two_night_stay(db_conn, total_rooms=2)
    create_hold(
        db_conn,
        hotel_id,
        room_type_id,
        _CHECK_IN,
        _CHECK_OUT,
        2,
        _NOW,
        idempotency_key="exceed-total-first",
    )

    with pytest.raises(InsufficientInventoryError):
        create_hold(
            db_conn,
            hotel_id,
            room_type_id,
            _CHECK_IN,
            _CHECK_OUT,
            1,
            _NOW,
            idempotency_key="exceed-total-second",
        )

    rows = db_conn.execute(
        "SELECT held, reserved, total FROM room_night_inventory rni "
        "JOIN allotments a ON a.id = rni.allotment_id "
        "WHERE a.hotel_id = %s AND a.room_type_id = %s",
        (hotel_id, room_type_id),
    ).fetchall()
    assert all(held + reserved <= total for held, reserved, total in rows)
    assert all(held == 2 for held, _reserved, _total in rows)


def test_create_hold_expiry_follows_the_hold_window_ladder(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = _seed_two_night_stay(db_conn, total_rooms=3)
    far_check_in = date(2026, 12, 1)  # far beyond 15 days from _NOW
    seed_allotment_nights(
        db_conn, hotel_id, room_type_id, far_check_in, nights=1, total_rooms=3
    )

    hold_id = create_hold(
        db_conn,
        hotel_id,
        room_type_id,
        far_check_in,
        date(2026, 12, 2),
        1,
        _NOW,
        idempotency_key="expiry-ladder",
    )

    row = db_conn.execute(
        "SELECT expires_at, requires_full_payment FROM holds WHERE id = %s",
        (hold_id,),
    ).fetchone()
    assert row is not None
    assert row[0] - _NOW == timedelta(hours=48)
    assert row[1] is False


def test_create_hold_within_48h_saves_requires_full_payment(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    near_check_in = date(2026, 6, 2)  # 24h after _NOW: inside the 48h window
    seed_allotment_nights(
        db_conn, hotel_id, room_type_id, near_check_in, nights=1, total_rooms=3
    )

    hold_id = create_hold(
        db_conn,
        hotel_id,
        room_type_id,
        near_check_in,
        date(2026, 6, 3),
        1,
        _NOW,
        idempotency_key="within-48h",
    )

    row = db_conn.execute(
        "SELECT requires_full_payment FROM holds WHERE id = %s", (hold_id,)
    ).fetchone()
    assert row is not None
    assert row[0] is True


def test_create_hold_same_idempotency_key_twice_returns_same_hold(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = _seed_two_night_stay(db_conn, total_rooms=3)

    first_id = create_hold(
        db_conn,
        hotel_id,
        room_type_id,
        _CHECK_IN,
        _CHECK_OUT,
        2,
        _NOW,
        idempotency_key="retry-key",
    )
    second_id = create_hold(
        db_conn,
        hotel_id,
        room_type_id,
        _CHECK_IN,
        _CHECK_OUT,
        2,
        _NOW,
        idempotency_key="retry-key",
    )

    assert first_id == second_id
    rows = db_conn.execute(
        "SELECT held, reserved FROM room_night_inventory rni "
        "JOIN allotments a ON a.id = rni.allotment_id "
        "WHERE a.hotel_id = %s AND a.room_type_id = %s",
        (hotel_id, room_type_id),
    ).fetchall()
    # held is 2, not 4 — the retry did not create a second hold.
    assert rows == [(2, 0), (2, 0)]
    hold_count = db_conn.execute(
        "SELECT count(*) FROM holds WHERE idempotency_key = %s", ("retry-key",)
    ).fetchone()
    assert hold_count == (1,)


def test_confirm_hold_moves_held_to_reserved(db_conn: psycopg.Connection[Any]) -> None:
    hotel_id, room_type_id = _seed_two_night_stay(db_conn, total_rooms=3)
    hold_id = create_hold(
        db_conn,
        hotel_id,
        room_type_id,
        _CHECK_IN,
        _CHECK_OUT,
        2,
        _NOW,
        idempotency_key="confirm-moves",
    )

    confirm_hold(db_conn, hold_id, _NOW, payment_received_in_full=True)

    rows = db_conn.execute(
        "SELECT held, reserved FROM room_night_inventory rni "
        "JOIN allotments a ON a.id = rni.allotment_id "
        "WHERE a.hotel_id = %s AND a.room_type_id = %s",
        (hotel_id, room_type_id),
    ).fetchall()
    assert rows == [(0, 2), (0, 2)]


def test_confirm_hold_raises_hold_not_found(db_conn: psycopg.Connection[Any]) -> None:
    with pytest.raises(HoldNotFoundError):
        confirm_hold(db_conn, 999_999, _NOW, payment_received_in_full=True)


def test_confirm_hold_raises_when_already_confirmed(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = _seed_two_night_stay(db_conn, total_rooms=3)
    hold_id = create_hold(
        db_conn,
        hotel_id,
        room_type_id,
        _CHECK_IN,
        _CHECK_OUT,
        1,
        _NOW,
        idempotency_key="already-confirmed",
    )
    confirm_hold(db_conn, hold_id, _NOW, payment_received_in_full=True)

    with pytest.raises(HoldAlreadyResolvedError):
        confirm_hold(db_conn, hold_id, _NOW, payment_received_in_full=True)


def test_confirm_hold_raises_when_expired(db_conn: psycopg.Connection[Any]) -> None:
    hotel_id, room_type_id = _seed_two_night_stay(db_conn, total_rooms=3)
    hold_id = create_hold(
        db_conn,
        hotel_id,
        room_type_id,
        _CHECK_IN,
        _CHECK_OUT,
        1,
        _NOW,
        idempotency_key="confirm-expired",
    )

    row = db_conn.execute(
        "SELECT expires_at FROM holds WHERE id = %s", (hold_id,)
    ).fetchone()
    assert row is not None
    past_expiry = row[0]

    with pytest.raises(HoldExpiredError):
        confirm_hold(db_conn, hold_id, past_expiry, payment_received_in_full=True)


def test_confirm_hold_rejects_without_full_payment_when_required(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    near_check_in = date(2026, 6, 2)  # inside the 48h window
    seed_allotment_nights(
        db_conn, hotel_id, room_type_id, near_check_in, nights=1, total_rooms=3
    )
    hold_id = create_hold(
        db_conn,
        hotel_id,
        room_type_id,
        near_check_in,
        date(2026, 6, 3),
        1,
        _NOW,
        idempotency_key="reject-without-payment",
    )

    with pytest.raises(FullPaymentRequiredError):
        confirm_hold(db_conn, hold_id, _NOW, payment_received_in_full=False)

    # Rejected before any mutation: still held, not reserved, not confirmed.
    rows = db_conn.execute(
        "SELECT held, reserved FROM room_night_inventory rni "
        "JOIN allotments a ON a.id = rni.allotment_id "
        "WHERE a.hotel_id = %s AND a.room_type_id = %s",
        (hotel_id, room_type_id),
    ).fetchall()
    assert rows == [(1, 0)]
    confirmed_at = db_conn.execute(
        "SELECT confirmed_at FROM holds WHERE id = %s", (hold_id,)
    ).fetchone()
    assert confirmed_at == (None,)


def test_confirm_hold_succeeds_with_full_payment_when_required(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    near_check_in = date(2026, 6, 2)  # inside the 48h window
    seed_allotment_nights(
        db_conn, hotel_id, room_type_id, near_check_in, nights=1, total_rooms=3
    )
    hold_id = create_hold(
        db_conn,
        hotel_id,
        room_type_id,
        near_check_in,
        date(2026, 6, 3),
        1,
        _NOW,
        idempotency_key="accept-with-payment",
    )

    confirm_hold(db_conn, hold_id, _NOW, payment_received_in_full=True)

    rows = db_conn.execute(
        "SELECT held, reserved FROM room_night_inventory rni "
        "JOIN allotments a ON a.id = rni.allotment_id "
        "WHERE a.hotel_id = %s AND a.room_type_id = %s",
        (hotel_id, room_type_id),
    ).fetchall()
    assert rows == [(0, 1)]


def test_release_hold_decrements_held(db_conn: psycopg.Connection[Any]) -> None:
    hotel_id, room_type_id = _seed_two_night_stay(db_conn, total_rooms=3)
    hold_id = create_hold(
        db_conn,
        hotel_id,
        room_type_id,
        _CHECK_IN,
        _CHECK_OUT,
        2,
        _NOW,
        idempotency_key="release-decrements",
    )

    release_hold(db_conn, hold_id, _NOW)

    rows = db_conn.execute(
        "SELECT held, reserved FROM room_night_inventory rni "
        "JOIN allotments a ON a.id = rni.allotment_id "
        "WHERE a.hotel_id = %s AND a.room_type_id = %s",
        (hotel_id, room_type_id),
    ).fetchall()
    assert rows == [(0, 0), (0, 0)]


def test_release_hold_raises_hold_not_found(db_conn: psycopg.Connection[Any]) -> None:
    with pytest.raises(HoldNotFoundError):
        release_hold(db_conn, 999_999, _NOW)


def test_release_hold_raises_when_already_released(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = _seed_two_night_stay(db_conn, total_rooms=3)
    hold_id = create_hold(
        db_conn,
        hotel_id,
        room_type_id,
        _CHECK_IN,
        _CHECK_OUT,
        1,
        _NOW,
        idempotency_key="release-already-released",
    )
    release_hold(db_conn, hold_id, _NOW)

    with pytest.raises(HoldAlreadyResolvedError):
        release_hold(db_conn, hold_id, _NOW)


def test_release_hold_raises_when_already_confirmed(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = _seed_two_night_stay(db_conn, total_rooms=3)
    hold_id = create_hold(
        db_conn,
        hotel_id,
        room_type_id,
        _CHECK_IN,
        _CHECK_OUT,
        1,
        _NOW,
        idempotency_key="release-already-confirmed",
    )
    confirm_hold(db_conn, hold_id, _NOW, payment_received_in_full=True)

    with pytest.raises(HoldAlreadyResolvedError):
        release_hold(db_conn, hold_id, _NOW)


def test_release_hold_raises_when_a_night_is_missing_from_the_range(
    db_conn: psycopg.Connection[Any],
) -> None:
    """A room_night_inventory row disappearing after the hold was created
    (e.g. an operational data fix) must fail release cleanly — never
    silently decrement held on fewer nights than it was incremented on.
    """
    hotel_id, room_type_id = _seed_two_night_stay(db_conn, total_rooms=3)
    hold_id = create_hold(
        db_conn,
        hotel_id,
        room_type_id,
        _CHECK_IN,
        _CHECK_OUT,
        2,
        _NOW,
        idempotency_key="release-missing-night",
    )
    db_conn.execute(
        "DELETE FROM room_night_inventory WHERE stay_date = %s", (_CHECK_IN,)
    )

    with pytest.raises(AllotmentNotFoundError):
        release_hold(db_conn, hold_id, _NOW)

    # No partial mutation: the surviving night is untouched, and the hold
    # was never marked released.
    remaining_night = db_conn.execute(
        "SELECT held, reserved FROM room_night_inventory WHERE stay_date = %s",
        (_CHECK_IN + timedelta(days=1),),
    ).fetchone()
    assert remaining_night == (2, 0)
    released_at = db_conn.execute(
        "SELECT released_at FROM holds WHERE id = %s", (hold_id,)
    ).fetchone()
    assert released_at == (None,)


def test_adjust_room_nights_raises_when_a_night_is_missing_from_the_range(
    db_conn: psycopg.Connection[Any],
) -> None:
    """Direct test of _adjust_room_nights' own rowcount defense — a
    backstop for any caller that skips _lock_nights_for_update, which
    every current caller in this module already calls first.
    """
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    seed_allotment_nights(
        db_conn, hotel_id, room_type_id, _CHECK_IN, nights=1, total_rooms=5
    )

    with pytest.raises(RoomNightCountMismatchError):
        _adjust_room_nights(
            db_conn,
            hotel_id,
            room_type_id,
            _CHECK_IN,
            _CHECK_OUT,
            held_delta=1,
            reserved_delta=0,
            expected_nights=2,
        )
