"""Migration 0036 against a real Postgres: the re-engagement template as a
staff reply of kind 'template' (staff notification step 3, PR C) -- only the
holder queues one, it can name a hotel, the failure reason window_open is
allowed, and what the agent role may read of it."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from tests.integration._seed import seed_hotel, seed_staff_template_reply
from tests.integration.test_staff_replies import (
    _end_takeover,
    _queue_as,
    _reset,
    _seed_user,
    _sign_in,
    _taken_over,
)

pytestmark = pytest.mark.usefixtures("db_conn")

_QUEUE_TEMPLATE = "SELECT staff_queue_template_reply(%s, %s)"


def _queue_template_as(
    conn: psycopg.Connection[Any],
    user_id: str,
    conversation_id: int,
    hotel_id: int | None,
) -> int:
    _sign_in(conn, user_id)
    try:
        row = conn.execute(_QUEUE_TEMPLATE, (conversation_id, hotel_id)).fetchone()
    finally:
        _reset(conn)
    assert row is not None
    return int(row[0])


@pytest.mark.parametrize("role", ["admin", "sales"])
def test_the_holder_queues_a_template_naming_a_hotel(
    db_conn: psycopg.Connection[Any], role: str
) -> None:
    conversation_id, takeover_id, holder = _taken_over(db_conn, role=role)
    hotel_id = seed_hotel(db_conn)

    reply_id = _queue_template_as(db_conn, holder, conversation_id, hotel_id)

    assert db_conn.execute(
        "SELECT takeover_id, conversation_id, sent_by::text, kind, "
        "template_hotel_id, claimed_at FROM staff_replies WHERE id = %s",
        (reply_id,),
    ).fetchone() == (takeover_id, conversation_id, holder, "template", hotel_id, None)


def test_a_template_may_name_no_hotel(db_conn: psycopg.Connection[Any]) -> None:
    conversation_id, _, holder = _taken_over(db_conn)

    reply_id = _queue_template_as(db_conn, holder, conversation_id, None)

    assert db_conn.execute(
        "SELECT kind, template_hotel_id FROM staff_replies WHERE id = %s", (reply_id,)
    ).fetchone() == ("template", None)


def test_a_text_reply_is_still_kind_text_with_no_hotel(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, _, holder = _taken_over(db_conn)

    reply_id = _queue_as(db_conn, holder, conversation_id, "hello")

    assert db_conn.execute(
        "SELECT kind, template_hotel_id FROM staff_replies WHERE id = %s", (reply_id,)
    ).fetchone() == ("text", None)


@pytest.mark.parametrize("role", ["admin", "sales"])
def test_nobody_but_the_holder_queues_a_template(
    db_conn: psycopg.Connection[Any], role: str
) -> None:
    conversation_id, _, _ = _taken_over(db_conn)
    other = _seed_user(db_conn, role=role)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _queue_template_as(db_conn, other, conversation_id, None)
    assert db_conn.execute("SELECT count(*) FROM staff_replies").fetchone() == (0,)


def test_no_template_to_a_conversation_nobody_holds(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, takeover_id, holder = _taken_over(db_conn)
    _end_takeover(db_conn, takeover_id)

    with pytest.raises(psycopg.errors.NoDataFound):
        _queue_template_as(db_conn, holder, conversation_id, None)


@pytest.mark.parametrize(("role", "is_active"), [("sales", False), (None, True)])
def test_anyone_who_is_not_active_staff_cannot_queue_a_template(
    db_conn: psycopg.Connection[Any], role: str | None, is_active: bool
) -> None:
    conversation_id, _, _ = _taken_over(db_conn)
    outsider = _seed_user(db_conn, role=role, is_active=is_active)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _queue_template_as(db_conn, outsider, conversation_id, None)


def test_a_template_must_name_a_hotel_that_exists(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, _, holder = _taken_over(db_conn)

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _queue_template_as(db_conn, holder, conversation_id, 999_999)


def test_deleting_a_hotel_keeps_the_template_row_without_a_hotel(
    db_conn: psycopg.Connection[Any],
) -> None:
    _, takeover_id, _ = _taken_over(db_conn)
    hotel_id = seed_hotel(db_conn)
    reply_id = seed_staff_template_reply(db_conn, takeover_id, hotel_id=hotel_id)

    db_conn.execute("DELETE FROM hotels WHERE id = %s", (hotel_id,))

    assert db_conn.execute(
        "SELECT kind, template_hotel_id FROM staff_replies WHERE id = %s", (reply_id,)
    ).fetchone() == ("template", None)


def test_only_a_template_names_a_hotel_and_the_kind_must_be_known(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, takeover_id, holder = _taken_over(db_conn)
    hotel_id = seed_hotel(db_conn)
    insert = (
        "INSERT INTO staff_replies (takeover_id, conversation_id, sent_by, body, "
        "kind, template_hotel_id) VALUES (%s, %s, %s, 'x', %s, %s)"
    )

    with pytest.raises(psycopg.errors.CheckViolation):
        db_conn.execute(
            insert, (takeover_id, conversation_id, holder, "text", hotel_id)
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        db_conn.execute(insert, (takeover_id, conversation_id, holder, "voice", None))


def test_a_direct_insert_of_a_template_still_needs_the_holder(
    db_conn: psycopg.Connection[Any],
) -> None:
    """The policies alone refuse a template written around the function."""
    conversation_id, takeover_id, _ = _taken_over(db_conn)
    other = _seed_user(db_conn, role="admin")
    _sign_in(db_conn, other)
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute(
                "INSERT INTO staff_replies (takeover_id, conversation_id, sent_by, "
                "body, kind) VALUES (%s, %s, %s, 'x', 'template')",
                (takeover_id, conversation_id, other),
            )
    finally:
        _reset(db_conn)


def test_staff_cannot_change_a_replys_kind_or_hotel_afterwards(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, _, holder = _taken_over(db_conn)
    _queue_as(db_conn, holder, conversation_id, "hello")
    _sign_in(db_conn, holder)
    try:
        for statement in (
            "UPDATE staff_replies SET kind = 'template'",
            "UPDATE staff_replies SET template_hotel_id = NULL",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db_conn.execute(statement)
    finally:
        _reset(db_conn)


@pytest.mark.parametrize("reason", ["outside_window", "send_failed", "window_open"])
def test_every_failure_reason_the_agent_records_is_allowed(
    db_conn: psycopg.Connection[Any], reason: str
) -> None:
    _, takeover_id, _ = _taken_over(db_conn)
    reply_id = seed_staff_template_reply(db_conn, takeover_id)

    db_conn.execute(
        "UPDATE staff_replies SET claimed_at = now(), failed_at = now(), "
        "failure_reason = %s WHERE id = %s",
        (reason, reply_id),
    )


def test_any_other_failure_reason_is_refused(db_conn: psycopg.Connection[Any]) -> None:
    _, takeover_id, _ = _taken_over(db_conn)
    reply_id = seed_staff_template_reply(db_conn, takeover_id)

    with pytest.raises(psycopg.errors.CheckViolation):
        db_conn.execute(
            "UPDATE staff_replies SET claimed_at = now(), failed_at = now(), "
            "failure_reason = 'because' WHERE id = %s",
            (reply_id,),
        )


def test_the_agent_reads_the_kind_and_the_hotel_but_never_writes_them(
    db_conn: psycopg.Connection[Any], agent_database_url: str
) -> None:
    _, takeover_id, _ = _taken_over(db_conn)
    hotel_id = seed_hotel(db_conn)
    reply_id = seed_staff_template_reply(db_conn, takeover_id, hotel_id=hotel_id)

    with psycopg.connect(agent_database_url, autocommit=True) as agent:
        row = agent.execute(
            "SELECT id, kind, template_hotel_id FROM staff_replies"
        ).fetchone()
        for statement in (
            "UPDATE staff_replies SET kind = 'text'",
            "UPDATE staff_replies SET template_hotel_id = NULL",
            "SELECT staff_queue_template_reply(1, NULL)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                agent.execute(statement)

    assert row == (reply_id, "template", hotel_id)
