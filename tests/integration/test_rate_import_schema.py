"""Migration 0037 against a real Postgres: the price-import tables, their
constraints and the guards -- the status graph, the stamps only the guard
writes, approval_seq, rows frozen outside a draft, and nights written once.

Everything here runs on the backend path: the owner connection, acting for
a staff member through app.actor_id. What each role may do is
test_rate_import_access.py's subject.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import psycopg
import pytest
from psycopg import sql

from tests.integration._rate_import_seed import (
    RATE_IMPORT_FINGERPRINT,
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
from tests.integration._seed import seed_hotel, seed_hotel_and_room_type, seed_room_type

pytestmark = pytest.mark.usefixtures("db_conn")

_STATUSES = ("draft", "validated", "approved", "disabled", "rejected")
_LEGAL_TRANSITIONS = frozenset(
    {
        ("draft", "validated"),
        ("draft", "rejected"),
        ("validated", "draft"),
        ("validated", "approved"),
        ("validated", "rejected"),
        ("approved", "disabled"),
        ("disabled", "approved"),
    }
)
_ILLEGAL_TRANSITIONS = [
    (old, new)
    for old in _STATUSES
    for new in _STATUSES
    if old != new and (old, new) not in _LEGAL_TRANSITIONS
]
# Every column only the guard writes, in the order the tests index them.
_STAMPS_SQL = (
    "SELECT validated_fingerprint, validated_by, validated_at, approved_by, "
    "approved_at, approval_seq, disabled_by, disabled_at, rejected_by, rejected_at "
    "FROM rate_import_batches WHERE id = %s"
)
_SET_REVIEW_CHOICES = (
    "UPDATE rate_import_batches SET period_end_inclusive = %s, "
    "period_years_confirmed = %s WHERE id = %s"
)


def _approver(conn: psycopg.Connection[Any]) -> str:
    """An active admin who can view cost, acting for the rest of the test."""
    user_id = seed_app_user(conn, role="admin", can_view_cost=True)
    act_as(conn, user_id)
    return user_id


def _batch_field(conn: psycopg.Connection[Any], batch_id: int, column: str) -> Any:
    row = conn.execute(
        sql.SQL("SELECT {} FROM rate_import_batches WHERE id = %s").format(
            sql.Identifier(column)
        ),
        (batch_id,),
    ).fetchone()
    assert row is not None
    return row[0]


def _stamps(conn: psycopg.Connection[Any], batch_id: int) -> tuple[Any, ...]:
    row = conn.execute(_STAMPS_SQL, (batch_id,)).fetchone()
    assert row is not None
    return tuple(row)


# --- creating a batch ----------------------------------------------------


def test_a_new_batch_is_a_draft_stamped_with_its_creator(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    creator = _approver(db_conn)

    batch_id = seed_rate_import_batch(db_conn, hotel_id)

    row = db_conn.execute(
        "SELECT status, price_type, created_by::text, period_end_inclusive, "
        "period_years_confirmed FROM rate_import_batches WHERE id = %s",
        (batch_id,),
    ).fetchone()
    assert row == ("draft", "sell", creator, None, False)
    assert _stamps(db_conn, batch_id) == (None,) * 10


def test_a_batch_cannot_be_created_in_any_status_but_draft(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)

    with pytest.raises(psycopg.errors.RaiseException, match="starts as a draft"):
        db_conn.execute(
            "INSERT INTO rate_import_batches (hotel_id, price_type, status) "
            "VALUES (%s, 'sell', 'approved')",
            (hotel_id,),
        )


def test_a_batch_cannot_name_someone_else_as_its_creator(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    creator = _approver(db_conn)
    other = seed_app_user(db_conn, role="admin")

    row = db_conn.execute(
        "INSERT INTO rate_import_batches (hotel_id, price_type, created_by) "
        "VALUES (%s, 'sell', %s) RETURNING created_by::text",
        (hotel_id, other),
    ).fetchone()

    assert row == (creator,)


def test_a_write_with_no_known_actor_is_refused(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)

    with pytest.raises(psycopg.errors.RaiseException, match="acting user"):
        seed_rate_import_batch(db_conn, hotel_id)


def test_only_selling_prices_are_importable(db_conn: psycopg.Connection[Any]) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)

    with pytest.raises(psycopg.errors.CheckViolation):
        db_conn.execute(
            "INSERT INTO rate_import_batches (hotel_id, price_type) "
            "VALUES (%s, 'cost')",
            (hotel_id,),
        )


# --- the status graph ------------------------------------------------------


def test_the_whole_path_stamps_each_step(db_conn: psycopg.Connection[Any]) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    actor = _approver(db_conn)
    batch_id = seed_rate_import_batch(db_conn, hotel_id)

    validate_rate_import_batch(db_conn, batch_id)
    fingerprint, validated_by, validated_at, *rest = _stamps(db_conn, batch_id)
    assert (fingerprint, str(validated_by)) == (RATE_IMPORT_FINGERPRINT, actor)
    assert validated_at is not None
    assert rest == [None] * 7

    set_rate_import_status(db_conn, batch_id, "approved")
    _, _, _, approved_by, approved_at, approval_seq, *rest = _stamps(db_conn, batch_id)
    assert str(approved_by) == actor
    assert approved_at is not None
    assert approval_seq is not None
    assert rest == [None] * 4

    set_rate_import_status(db_conn, batch_id, "disabled")
    stamps = _stamps(db_conn, batch_id)
    assert str(stamps[6]) == actor
    assert stamps[7] is not None
    assert stamps[5] == approval_seq


def test_re_enabling_keeps_the_original_approval_seq(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)
    first = seed_rate_import_batch_in(db_conn, hotel_id, "disabled")
    first_seq = _batch_field(db_conn, first, "approval_seq")
    later = seed_rate_import_batch_in(db_conn, hotel_id, "approved")

    set_rate_import_status(db_conn, first, "approved")

    stamps = _stamps(db_conn, first)
    assert stamps[5] == first_seq
    assert stamps[6:8] == (None, None)
    assert _batch_field(db_conn, later, "approval_seq") > first_seq


def test_each_approval_draws_a_higher_approval_seq(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)

    sequence = [
        _batch_field(
            db_conn,
            seed_rate_import_batch_in(db_conn, hotel_id, "approved"),
            "approval_seq",
        )
        for _ in range(3)
    ]

    assert sequence == sorted(set(sequence))


@pytest.mark.parametrize(("old", "new"), _ILLEGAL_TRANSITIONS)
def test_a_transition_outside_the_graph_is_refused(
    db_conn: psycopg.Connection[Any], old: str, new: str
) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)
    batch_id = seed_rate_import_batch_in(db_conn, hotel_id, old)

    with pytest.raises(psycopg.errors.RaiseException, match="cannot go from"):
        set_rate_import_status(db_conn, batch_id, new)
    assert _batch_field(db_conn, batch_id, "status") == old


@pytest.mark.parametrize("status", ["validated", "approved", "disabled", "rejected"])
def test_only_a_draft_can_be_edited(
    db_conn: psycopg.Connection[Any], status: str
) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)
    batch_id = seed_rate_import_batch_in(db_conn, hotel_id, status)

    with pytest.raises(psycopg.errors.RaiseException, match="only a draft"):
        db_conn.execute(
            "UPDATE rate_import_batches SET period_end_inclusive = false WHERE id = %s",
            (batch_id,),
        )


def test_a_batch_cannot_move_to_another_hotel(db_conn: psycopg.Connection[Any]) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    other_hotel = seed_hotel(db_conn, hotel_name="Other Test Hotel")
    _approver(db_conn)
    batch_id = seed_rate_import_batch(db_conn, hotel_id)

    with pytest.raises(psycopg.errors.RaiseException, match="hotel or price type"):
        db_conn.execute(
            "UPDATE rate_import_batches SET hotel_id = %s WHERE id = %s",
            (other_hotel, batch_id),
        )


# --- validation ------------------------------------------------------------


@pytest.mark.parametrize(
    ("period_end_inclusive", "period_years_confirmed", "fingerprint"),
    [
        # Neither review choice settled.
        (None, False, RATE_IMPORT_FINGERPRINT),
        # The period-end choice still open.
        (None, True, RATE_IMPORT_FINGERPRINT),
        # The years not confirmed.
        (True, False, RATE_IMPORT_FINGERPRINT),
        # No fingerprint, and one that is not a sha256.
        (True, True, None),
        (True, True, "abc"),
    ],
)
def test_validating_needs_both_review_choices_and_a_fingerprint(
    db_conn: psycopg.Connection[Any],
    period_end_inclusive: bool | None,
    period_years_confirmed: bool,
    fingerprint: str | None,
) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)
    batch_id = seed_rate_import_batch(db_conn, hotel_id)
    db_conn.execute(
        _SET_REVIEW_CHOICES, (period_end_inclusive, period_years_confirmed, batch_id)
    )

    with pytest.raises(psycopg.errors.CheckViolation):
        db_conn.execute(
            "UPDATE rate_import_batches SET status = 'validated', "
            "validated_fingerprint = %s WHERE id = %s",
            (fingerprint, batch_id),
        )
    assert _batch_field(db_conn, batch_id, "status") == "draft"


def test_a_review_choice_cannot_change_in_the_validating_statement(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)
    batch_id = seed_rate_import_batch(db_conn, hotel_id)

    with pytest.raises(psycopg.errors.RaiseException, match="review choices"):
        db_conn.execute(
            "UPDATE rate_import_batches SET status = 'validated', "
            "validated_fingerprint = %s, period_end_inclusive = true, "
            "period_years_confirmed = true WHERE id = %s",
            (RATE_IMPORT_FINGERPRINT, batch_id),
        )


def test_returning_to_draft_clears_the_validation_and_reopens_the_choices(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)
    batch_id = seed_rate_import_batch_in(db_conn, hotel_id, "validated")

    db_conn.execute(
        "UPDATE rate_import_batches SET status = 'draft', "
        "period_end_inclusive = false WHERE id = %s",
        (batch_id,),
    )

    assert _stamps(db_conn, batch_id) == (None,) * 10
    assert _batch_field(db_conn, batch_id, "period_end_inclusive") is False


def test_a_rejected_batch_keeps_the_validation_it_had(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    actor = _approver(db_conn)
    batch_id = seed_rate_import_batch_in(db_conn, hotel_id, "validated")

    set_rate_import_status(db_conn, batch_id, "rejected")

    stamps = _stamps(db_conn, batch_id)
    assert stamps[0] == RATE_IMPORT_FINGERPRINT
    assert str(stamps[8]) == actor
    assert stamps[9] is not None


# --- the stamps are the guard's --------------------------------------------


def test_a_caller_cannot_write_the_stamps(db_conn: psycopg.Connection[Any]) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    creator = _approver(db_conn)
    other = seed_app_user(db_conn, role="admin", can_view_cost=True)
    batch_id = seed_rate_import_batch(db_conn, hotel_id)

    db_conn.execute(
        "UPDATE rate_import_batches SET created_by = %(other)s, "
        "validated_fingerprint = %(fingerprint)s, validated_by = %(other)s, "
        "validated_at = now(), approved_by = %(other)s, approved_at = now(), "
        "approval_seq = 1, disabled_by = %(other)s, disabled_at = now(), "
        "rejected_by = %(other)s, rejected_at = now() WHERE id = %(id)s",
        {"other": other, "fingerprint": RATE_IMPORT_FINGERPRINT, "id": batch_id},
    )

    assert _stamps(db_conn, batch_id) == (None,) * 10
    assert str(_batch_field(db_conn, batch_id, "created_by")) == creator


def test_a_caller_cannot_choose_the_approval_seq_or_the_approver(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    actor = _approver(db_conn)
    other = seed_app_user(db_conn, role="admin", can_view_cost=True)
    earlier = seed_rate_import_batch_in(db_conn, hotel_id, "approved")
    batch_id = seed_rate_import_batch_in(db_conn, hotel_id, "validated")

    db_conn.execute(
        "UPDATE rate_import_batches SET status = 'approved', approval_seq = 0, "
        "approved_by = %s, approved_at = '2020-01-01T00:00:00Z' WHERE id = %s",
        (other, batch_id),
    )

    row = db_conn.execute(
        "SELECT approved_by::text, approved_at > '2026-01-01T00:00:00Z', approval_seq "
        "FROM rate_import_batches WHERE id = %s",
        (batch_id,),
    ).fetchone()
    assert row is not None
    assert row[:2] == (actor, True)
    assert row[2] > _batch_field(db_conn, earlier, "approval_seq")


# --- who may approve ---------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "can_view_cost", "is_active"),
    [("admin", False, True), ("sales", True, True), ("admin", True, False)],
)
@pytest.mark.parametrize("from_status", ["validated", "disabled"])
def test_only_an_active_admin_who_can_view_cost_approves(
    db_conn: psycopg.Connection[Any],
    role: str,
    can_view_cost: bool,
    is_active: bool,
    from_status: str,
) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)
    batch_id = seed_rate_import_batch_in(db_conn, hotel_id, from_status)
    act_as(
        db_conn,
        seed_app_user(
            db_conn, role=role, can_view_cost=can_view_cost, is_active=is_active
        ),
    )

    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="approves"):
        set_rate_import_status(db_conn, batch_id, "approved")
    assert _batch_field(db_conn, batch_id, "status") == from_status


# --- rows --------------------------------------------------------------------


@pytest.mark.parametrize("status", ["validated", "approved", "disabled", "rejected"])
def test_rows_change_only_while_their_batch_is_a_draft(
    db_conn: psycopg.Connection[Any], status: str
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)
    batch_id = seed_rate_import_batch(db_conn, hotel_id)
    row_id = seed_rate_import_row(db_conn, batch_id, hotel_id, room_type_id)
    advance_rate_import_batch(db_conn, batch_id, status)

    with pytest.raises(psycopg.errors.RaiseException, match="is a draft"):
        seed_rate_import_row(db_conn, batch_id, hotel_id, room_type_id)
    with pytest.raises(psycopg.errors.RaiseException, match="is a draft"):
        db_conn.execute(
            "UPDATE rate_import_rows SET weekday_price_halalas = 1 WHERE id = %s",
            (row_id,),
        )


def test_a_row_is_stamped_with_whoever_added_it_and_stays_so(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)
    batch_id = seed_rate_import_batch(db_conn, hotel_id)
    second_admin = seed_app_user(db_conn, role="admin")
    act_as(db_conn, second_admin)
    row_id = seed_rate_import_row(db_conn, batch_id, hotel_id, room_type_id)

    _approver(db_conn)
    db_conn.execute(
        "UPDATE rate_import_rows SET is_excluded = true WHERE id = %s", (row_id,)
    )

    row = db_conn.execute(
        "SELECT created_by::text, is_excluded FROM rate_import_rows WHERE id = %s",
        (row_id,),
    ).fetchone()
    assert row == (second_admin, True)


def test_a_row_cannot_move_to_another_batch(db_conn: psycopg.Connection[Any]) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)
    batch_id = seed_rate_import_batch(db_conn, hotel_id)
    other_batch = seed_rate_import_batch(db_conn, hotel_id)
    row_id = seed_rate_import_row(db_conn, batch_id, hotel_id, room_type_id)

    with pytest.raises(psycopg.errors.RaiseException, match="another batch"):
        db_conn.execute(
            "UPDATE rate_import_rows SET batch_id = %s WHERE id = %s",
            (other_batch, row_id),
        )


@pytest.mark.parametrize(
    ("weekday", "weekend", "is_closed"),
    [
        (None, None, False),
        (40_000, None, False),
        (None, 50_000, False),
        (0, 50_000, False),
        (40_000, -1, False),
        (40_000, 50_000, True),
    ],
)
def test_a_row_is_either_priced_in_full_or_closed(
    db_conn: psycopg.Connection[Any],
    weekday: int | None,
    weekend: int | None,
    is_closed: bool,
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)
    batch_id = seed_rate_import_batch(db_conn, hotel_id)

    with pytest.raises(psycopg.errors.CheckViolation):
        seed_rate_import_row(
            db_conn,
            batch_id,
            hotel_id,
            room_type_id,
            weekday_price_halalas=weekday,
            weekend_price_halalas=weekend,
            is_closed=is_closed,
        )


def test_a_closed_row_and_a_one_day_period_are_accepted(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)
    batch_id = seed_rate_import_batch(db_conn, hotel_id)

    seed_rate_import_row(
        db_conn,
        batch_id,
        hotel_id,
        room_type_id,
        period_start=date(2027, 2, 1),
        period_end=date(2027, 2, 1),
        weekday_price_halalas=None,
        weekend_price_halalas=None,
        is_closed=True,
    )

    assert db_conn.execute("SELECT count(*) FROM rate_import_rows").fetchone() == (1,)


def test_a_period_cannot_end_before_it_starts(db_conn: psycopg.Connection[Any]) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)
    batch_id = seed_rate_import_batch(db_conn, hotel_id)

    with pytest.raises(psycopg.errors.CheckViolation):
        seed_rate_import_row(
            db_conn,
            batch_id,
            hotel_id,
            room_type_id,
            period_start=date(2027, 1, 10),
            period_end=date(2027, 1, 9),
        )


def test_a_row_cannot_name_another_hotels_room_type_or_batch(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    other_hotel = seed_hotel(db_conn, hotel_name="Other Test Hotel")
    other_room_type = seed_room_type(db_conn, other_hotel)
    _approver(db_conn)
    batch_id = seed_rate_import_batch(db_conn, hotel_id)

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        seed_rate_import_row(db_conn, batch_id, hotel_id, other_room_type)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        seed_rate_import_row(db_conn, batch_id, other_hotel, other_room_type)


# --- nights ------------------------------------------------------------------


@pytest.mark.parametrize("status", ["draft", "approved", "disabled", "rejected"])
def test_nights_are_written_only_while_the_batch_is_validated(
    db_conn: psycopg.Connection[Any], status: str
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)
    batch_id = seed_rate_import_batch_in(db_conn, hotel_id, status)

    with pytest.raises(psycopg.errors.RaiseException, match="is validated"):
        seed_rate_import_night(db_conn, batch_id, hotel_id, room_type_id)


def test_a_validated_batch_takes_its_nights_and_is_then_approved(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)
    batch_id = seed_rate_import_batch_in(db_conn, hotel_id, "validated")

    seed_rate_import_night(db_conn, batch_id, hotel_id, room_type_id)
    set_rate_import_status(db_conn, batch_id, "approved")

    assert db_conn.execute(
        "SELECT stay_date, sell_price_halalas FROM rate_import_nights "
        "WHERE batch_id = %s",
        (batch_id,),
    ).fetchall() == [(date(2027, 1, 1), 40_000)]


@pytest.mark.parametrize("withdrawn_to", ["draft", "rejected"])
def test_a_batch_that_has_nights_can_only_be_approved(
    db_conn: psycopg.Connection[Any], withdrawn_to: str
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)
    batch_id = seed_rate_import_batch_in(db_conn, hotel_id, "validated")
    seed_rate_import_night(db_conn, batch_id, hotel_id, room_type_id)

    with pytest.raises(psycopg.errors.RaiseException, match="has nights"):
        set_rate_import_status(db_conn, batch_id, withdrawn_to)


def test_a_batch_has_one_price_per_room_type_and_night(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)
    batch_id = seed_rate_import_batch_in(db_conn, hotel_id, "validated")
    seed_rate_import_night(db_conn, batch_id, hotel_id, room_type_id)

    with pytest.raises(psycopg.errors.UniqueViolation):
        seed_rate_import_night(
            db_conn, batch_id, hotel_id, room_type_id, sell_price_halalas=41_000
        )


@pytest.mark.parametrize("price", [0, -100])
def test_a_night_price_is_positive(
    db_conn: psycopg.Connection[Any], price: int
) -> None:
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    _approver(db_conn)
    batch_id = seed_rate_import_batch_in(db_conn, hotel_id, "validated")

    with pytest.raises(psycopg.errors.CheckViolation):
        seed_rate_import_night(
            db_conn, batch_id, hotel_id, room_type_id, sell_price_halalas=price
        )


def test_a_night_cannot_name_another_hotels_room_type(
    db_conn: psycopg.Connection[Any],
) -> None:
    hotel_id, _ = seed_hotel_and_room_type(db_conn)
    other_hotel = seed_hotel(db_conn, hotel_name="Other Test Hotel")
    other_room_type = seed_room_type(db_conn, other_hotel)
    _approver(db_conn)
    batch_id = seed_rate_import_batch_in(db_conn, hotel_id, "validated")

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        seed_rate_import_night(db_conn, batch_id, hotel_id, other_room_type)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        seed_rate_import_night(db_conn, batch_id, other_hotel, other_room_type)
