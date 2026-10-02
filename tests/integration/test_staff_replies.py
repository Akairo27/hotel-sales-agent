"""Migration 0035 against a real Postgres: staff replies from the dashboard
(staff notification step 3, owner decisions 2026-10-02) -- only the holder
of the active takeover writes one, what the agent role may read and write,
the amounts audit, the link from messages, and erasure."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from tests.integration._seed import (
    seed_conversation,
    seed_escalation,
    seed_message,
    seed_staff_reply,
)

pytestmark = pytest.mark.usefixtures("db_conn")

_PHONE = "+966500000001"
_OTHER_PHONE = "+966500000002"
# WhatsApp's limit for a text message body (staff_replies'
# body_within_whatsapp_limit constraint).
_WHATSAPP_TEXT_LIMIT = 4096

_TAKE_OVER = "SELECT * FROM staff_take_over_conversation(%s)"
_QUEUE = "SELECT staff_queue_reply(%s, %s)"
_RECORD_AMOUNTS = "SELECT staff_reply_record_amounts(%s, %s)"
_AMOUNTS = [{"raw": "1,500", "halalas": 150_000}]
# Bypassing staff_queue_reply: the policies alone must refuse it.
_DIRECT_INSERT = (
    "INSERT INTO staff_replies (takeover_id, conversation_id, sent_by, body) "
    "VALUES (%s, %s, %s, 'hello')"
)


def _seed_user(
    conn: psycopg.Connection[Any],
    *,
    role: str | None,
    is_active: bool = True,
    can_view_cost: bool = False,
) -> str:
    """An auth.users identity, with an app_users row in `role` unless role
    is None (signed in, never provisioned)."""
    row = conn.execute("INSERT INTO auth.users DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    user_id = str(row[0])
    if role is not None:
        conn.execute(
            "INSERT INTO app_users (id, full_name, app_role, is_active, can_view_cost) "
            "VALUES (%s, 'Test User', %s, %s, %s)",
            (user_id, role, is_active, can_view_cost),
        )
    return user_id


def _sign_in(conn: psycopg.Connection[Any], user_id: str) -> None:
    conn.execute("SET SESSION AUTHORIZATION authenticated")
    conn.execute(sql.SQL("SET request.jwt.claim.sub = {}").format(sql.Literal(user_id)))


def _reset(conn: psycopg.Connection[Any]) -> None:
    conn.execute("RESET SESSION AUTHORIZATION")
    conn.execute("RESET request.jwt.claim.sub")


def _taken_over(
    conn: psycopg.Connection[Any], *, role: str = "sales", phone: str = _PHONE
) -> tuple[int, int, str]:
    """An escalated conversation taken over by a new `role` user through
    staff_take_over_conversation; returns the conversation id, the takeover
    id and the holder's id."""
    conversation_id = seed_conversation(conn, customer_phone=phone)
    seed_escalation(
        conn, conversation_id, reason="booking_requested", customer_phone=phone
    )
    holder = _seed_user(conn, role=role)
    _sign_in(conn, holder)
    row = conn.execute(_TAKE_OVER, (conversation_id,)).fetchone()
    _reset(conn)
    assert row is not None
    return conversation_id, int(row[0]), holder


def _end_takeover(conn: psycopg.Connection[Any], takeover_id: int) -> None:
    conn.execute(
        "UPDATE conversation_takeovers SET ended_at = now(), ended_by = taken_over_by, "
        "outcome = 'resolved' WHERE id = %s",
        (takeover_id,),
    )


def _queue_as(
    conn: psycopg.Connection[Any], user_id: str, conversation_id: int, body: str
) -> int:
    _sign_in(conn, user_id)
    try:
        row = conn.execute(_QUEUE, (conversation_id, body)).fetchone()
    finally:
        _reset(conn)
    assert row is not None
    return int(row[0])


def _audit_rows(conn: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT table_name, row_id, column_name, old_value, new_value, "
        "changed_by::text FROM audit_log ORDER BY id"
    ).fetchall()


# ---------------------------------------------------------------------------
# Staff: only the holder writes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "sales"])
def test_the_holder_queues_a_reply_in_their_own_name(
    db_conn: psycopg.Connection[Any], role: str
) -> None:
    conversation_id, takeover_id, holder = _taken_over(db_conn, role=role)

    reply_id = _queue_as(db_conn, holder, conversation_id, "Your room is ready")

    row = db_conn.execute(
        "SELECT takeover_id, conversation_id, sent_by::text, body, "
        "created_at IS NOT NULL, claimed_at, sent_at, failed_at, failure_reason "
        "FROM staff_replies WHERE id = %s",
        (reply_id,),
    ).fetchone()
    assert row == (
        takeover_id,
        conversation_id,
        holder,
        "Your room is ready",
        True,
        None,
        None,
        None,
        None,
    )


@pytest.mark.parametrize("role", ["admin", "sales"])
def test_nobody_but_the_holder_queues_a_reply(
    db_conn: psycopg.Connection[Any], role: str
) -> None:
    """Owner decision 2026-10-02: an admin who needs to reply ends the
    takeover and takes it over."""
    conversation_id, _, _ = _taken_over(db_conn)
    other = _seed_user(db_conn, role=role)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _queue_as(db_conn, other, conversation_id, "hello")
    assert db_conn.execute("SELECT count(*) FROM staff_replies").fetchone() == (0,)


def test_no_reply_to_a_conversation_nobody_holds(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, takeover_id, holder = _taken_over(db_conn)
    _end_takeover(db_conn, takeover_id)

    with pytest.raises(psycopg.errors.NoDataFound):
        _queue_as(db_conn, holder, conversation_id, "hello")


@pytest.mark.parametrize(("role", "is_active"), [("sales", False), (None, True)])
def test_anyone_who_is_not_active_staff_cannot_reply(
    db_conn: psycopg.Connection[Any], role: str | None, is_active: bool
) -> None:
    conversation_id, _, _ = _taken_over(db_conn)
    outsider = _seed_user(db_conn, role=role, is_active=is_active)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _queue_as(db_conn, outsider, conversation_id, "hello")


def test_a_holder_who_was_deactivated_cannot_reply(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, _, holder = _taken_over(db_conn)
    db_conn.execute("UPDATE app_users SET is_active = false WHERE id = %s", (holder,))

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _queue_as(db_conn, holder, conversation_id, "hello")


def test_a_direct_insert_cannot_use_someone_elses_name_or_takeover(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    conversation_id, takeover_id, holder = _taken_over(db_conn)
    other_conversation, other_takeover, _ = _taken_over(db_conn, phone=_OTHER_PHONE)
    attempts = [
        # In the holder's name, by someone else.
        (_seed_user(db_conn, role="admin"), takeover_id, conversation_id, holder),
        # The holder's own takeover, aimed at another conversation.
        (holder, takeover_id, other_conversation, holder),
        # Someone else's takeover, in the holder's own name.
        (holder, other_takeover, other_conversation, holder),
    ]

    for caller, attempt_takeover, attempt_conversation, named in attempts:
        sign_in_as(caller)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute(
                _DIRECT_INSERT, (attempt_takeover, attempt_conversation, named)
            )
        _reset(db_conn)


def test_a_reply_to_an_ended_takeover_cannot_be_inserted_directly(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    conversation_id, takeover_id, holder = _taken_over(db_conn)
    _end_takeover(db_conn, takeover_id)

    sign_in_as(holder)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute(_DIRECT_INSERT, (takeover_id, conversation_id, holder))


@pytest.mark.parametrize("body", ["", "   \n ", "x" * (_WHATSAPP_TEXT_LIMIT + 1)])
def test_a_blank_or_overlong_reply_is_refused(
    db_conn: psycopg.Connection[Any], body: str
) -> None:
    conversation_id, _, holder = _taken_over(db_conn)

    with pytest.raises(psycopg.errors.CheckViolation):
        _queue_as(db_conn, holder, conversation_id, body)


def test_a_reply_at_whatsapps_limit_is_accepted(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, _, holder = _taken_over(db_conn)

    assert _queue_as(db_conn, holder, conversation_id, "x" * _WHATSAPP_TEXT_LIMIT)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE staff_replies SET body = 'changed'",
        "UPDATE staff_replies SET claimed_at = now()",
        "UPDATE staff_replies SET sent_at = now()",
        "DELETE FROM staff_replies",
        "INSERT INTO staff_replies (takeover_id, conversation_id, sent_by, body, "
        "created_at) SELECT takeover_id, conversation_id, sent_by, 'x', now() "
        "FROM staff_replies",
    ],
)
def test_staff_cannot_change_delete_or_backdate_a_reply(
    db_conn: psycopg.Connection[Any],
    sign_in_as: Callable[[str], None],
    statement: str,
) -> None:
    conversation_id, _, holder = _taken_over(db_conn)
    _queue_as(db_conn, holder, conversation_id, "hello")

    sign_in_as(holder)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute(statement)


def test_every_active_staff_member_reads_every_reply_and_its_message(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    conversation_id, _, holder = _taken_over(db_conn)
    reply_id = _queue_as(db_conn, holder, conversation_id, "hello")
    db_conn.execute(
        "INSERT INTO messages (conversation_id, customer_phone, direction, body, "
        "staff_reply_id) VALUES (%s, %s, 'outbound', 'hello', %s)",
        (conversation_id, _PHONE, reply_id),
    )
    reader = _seed_user(db_conn, role="sales")

    sign_in_as(reader)
    replies = db_conn.execute("SELECT id, sent_by::text FROM staff_replies").fetchall()
    links = db_conn.execute(
        "SELECT staff_reply_id FROM messages WHERE staff_reply_id IS NOT NULL"
    ).fetchall()

    assert replies == [(reply_id, holder)]
    assert links == [(reply_id,)]


def test_inactive_users_read_no_replies(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    conversation_id, _, holder = _taken_over(db_conn)
    _queue_as(db_conn, holder, conversation_id, "hello")
    inactive = _seed_user(db_conn, role="admin", is_active=False)

    sign_in_as(inactive)

    assert db_conn.execute("SELECT id FROM staff_replies").fetchall() == []


# ---------------------------------------------------------------------------
# The agent role (hotel_agent)
# ---------------------------------------------------------------------------


def test_the_agent_reads_a_reply_to_send_it(
    db_conn: psycopg.Connection[Any], agent_database_url: str
) -> None:
    conversation_id, takeover_id, holder = _taken_over(db_conn)
    reply_id = _queue_as(db_conn, holder, conversation_id, "hello")

    with psycopg.connect(agent_database_url, autocommit=True) as agent:
        row = agent.execute(
            "SELECT id, takeover_id, conversation_id, body, claimed_at, sent_at, "
            "failed_at, failure_reason FROM staff_replies"
        ).fetchone()

    assert row == (
        reply_id,
        takeover_id,
        conversation_id,
        "hello",
        None,
        None,
        None,
        None,
    )


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT sent_by FROM staff_replies",
        "SELECT * FROM staff_replies",
        "UPDATE staff_replies SET body = 'changed'",
        "INSERT INTO staff_replies (takeover_id, conversation_id, sent_by, body) "
        "VALUES (1, 1, gen_random_uuid(), 'x')",
        "DELETE FROM staff_replies",
        "SELECT staff_queue_reply(1, 'x')",
    ],
)
def test_the_agent_never_sees_the_author_and_never_writes_a_reply(
    agent_database_url: str, statement: str
) -> None:
    with (
        psycopg.connect(agent_database_url, autocommit=True) as agent,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        agent.execute(statement)


def test_the_agent_claims_a_reply_once_then_records_it(
    db_conn: psycopg.Connection[Any], agent_database_url: str
) -> None:
    _, takeover_id, _ = _taken_over(db_conn)
    reply_id = seed_staff_reply(db_conn, takeover_id)
    claim = (
        "UPDATE staff_replies SET claimed_at = now() "
        "WHERE id = %s AND claimed_at IS NULL"
    )

    with psycopg.connect(agent_database_url, autocommit=True) as agent:
        first = agent.execute(claim, (reply_id,)).rowcount
        second = agent.execute(claim, (reply_id,)).rowcount
        recorded = agent.execute(
            "UPDATE staff_replies SET sent_at = now() WHERE id = %s", (reply_id,)
        ).rowcount
        again = agent.execute(
            "UPDATE staff_replies SET failed_at = now(), "
            "failure_reason = 'send_failed' WHERE id = %s",
            (reply_id,),
        ).rowcount

    assert (first, second, recorded, again) == (1, 0, 1, 0)


def test_the_agent_cannot_claim_a_reply_whose_takeover_ended(
    db_conn: psycopg.Connection[Any], agent_database_url: str
) -> None:
    _, takeover_id, _ = _taken_over(db_conn)
    reply_id = seed_staff_reply(db_conn, takeover_id)
    _end_takeover(db_conn, takeover_id)

    with psycopg.connect(agent_database_url, autocommit=True) as agent:
        claimed = agent.execute(
            "UPDATE staff_replies SET claimed_at = now() WHERE id = %s", (reply_id,)
        ).rowcount

    assert claimed == 0


def test_the_agent_records_the_outcome_even_if_the_takeover_ended_meanwhile(
    db_conn: psycopg.Connection[Any], agent_database_url: str
) -> None:
    _, takeover_id, _ = _taken_over(db_conn)
    reply_id = seed_staff_reply(db_conn, takeover_id)

    with psycopg.connect(agent_database_url, autocommit=True) as agent:
        agent.execute(
            "UPDATE staff_replies SET claimed_at = now() WHERE id = %s", (reply_id,)
        )
        _end_takeover(db_conn, takeover_id)
        recorded = agent.execute(
            "UPDATE staff_replies SET sent_at = now() WHERE id = %s", (reply_id,)
        ).rowcount

    assert recorded == 1


@pytest.mark.parametrize(
    "outcome",
    [
        "sent_at = now(), failed_at = now(), failure_reason = 'send_failed'",
        "failed_at = now()",
        "failed_at = now(), failure_reason = 'something_else'",
        "failure_reason = 'send_failed'",
    ],
)
def test_an_outcome_is_one_of_sent_or_failed_with_a_known_reason(
    db_conn: psycopg.Connection[Any], outcome: str
) -> None:
    _, takeover_id, _ = _taken_over(db_conn)
    reply_id = seed_staff_reply(db_conn, takeover_id)
    db_conn.execute(
        "UPDATE staff_replies SET claimed_at = now() WHERE id = %s", (reply_id,)
    )

    with pytest.raises(psycopg.errors.CheckViolation):
        db_conn.execute(
            sql.SQL("UPDATE staff_replies SET {outcome} WHERE id = {id}").format(
                outcome=sql.SQL(outcome), id=sql.Literal(reply_id)
            )
        )


def test_no_outcome_before_a_claim(db_conn: psycopg.Connection[Any]) -> None:
    _, takeover_id, _ = _taken_over(db_conn)
    reply_id = seed_staff_reply(db_conn, takeover_id)

    with pytest.raises(psycopg.errors.CheckViolation):
        db_conn.execute(
            "UPDATE staff_replies SET sent_at = now() WHERE id = %s", (reply_id,)
        )


# ---------------------------------------------------------------------------
# The amounts audit
# ---------------------------------------------------------------------------


def _claimed_reply(db_conn: psycopg.Connection[Any]) -> tuple[int, int, str]:
    """A claimed, unsent reply; returns its id, its conversation and its
    author."""
    conversation_id, takeover_id, holder = _taken_over(db_conn)
    reply_id = seed_staff_reply(db_conn, takeover_id, body="1,500 riyals")
    db_conn.execute(
        "UPDATE staff_replies SET claimed_at = now() WHERE id = %s", (reply_id,)
    )
    return reply_id, conversation_id, holder


def test_the_agent_records_a_replys_amounts_in_its_authors_name(
    db_conn: psycopg.Connection[Any], agent_database_url: str
) -> None:
    reply_id, conversation_id, holder = _claimed_reply(db_conn)

    with psycopg.connect(agent_database_url, autocommit=True) as agent:
        agent.execute(_RECORD_AMOUNTS, (reply_id, Jsonb(_AMOUNTS)))

    assert _audit_rows(db_conn) == [
        (
            "staff_replies",
            str(reply_id),
            "amounts",
            None,
            {"conversation_id": conversation_id, "amounts": _AMOUNTS},
            holder,
        )
    ]


def test_amounts_are_recorded_once_only(
    db_conn: psycopg.Connection[Any], agent_database_url: str
) -> None:
    reply_id, _, _ = _claimed_reply(db_conn)

    with psycopg.connect(agent_database_url, autocommit=True) as agent:
        agent.execute(_RECORD_AMOUNTS, (reply_id, Jsonb(_AMOUNTS)))
        with pytest.raises(psycopg.errors.UniqueViolation):
            agent.execute(_RECORD_AMOUNTS, (reply_id, Jsonb(_AMOUNTS)))

    assert len(_audit_rows(db_conn)) == 1


@pytest.mark.parametrize("state", ["unclaimed", "sent", "failed", "unknown"])
def test_amounts_are_recorded_only_for_a_claimed_unsent_reply(
    db_conn: psycopg.Connection[Any], agent_database_url: str, state: str
) -> None:
    reply_id, _, _ = _claimed_reply(db_conn)
    if state == "unclaimed":
        db_conn.execute("UPDATE staff_replies SET claimed_at = NULL")
    elif state == "sent":
        db_conn.execute("UPDATE staff_replies SET sent_at = now()")
    elif state == "failed":
        db_conn.execute(
            "UPDATE staff_replies SET failed_at = now(), failure_reason = 'send_failed'"
        )
    else:
        reply_id += 1

    with (
        psycopg.connect(agent_database_url, autocommit=True) as agent,
        pytest.raises(psycopg.errors.NoDataFound),
    ):
        agent.execute(_RECORD_AMOUNTS, (reply_id, Jsonb(_AMOUNTS)))
    assert _audit_rows(db_conn) == []


@pytest.mark.parametrize("amounts", [[], {"raw": "1,500"}, None])
def test_the_amounts_must_be_a_non_empty_list(
    db_conn: psycopg.Connection[Any], agent_database_url: str, amounts: Any
) -> None:
    reply_id, _, _ = _claimed_reply(db_conn)

    with (
        psycopg.connect(agent_database_url, autocommit=True) as agent,
        pytest.raises(psycopg.errors.InvalidParameterValue),
    ):
        agent.execute(
            _RECORD_AMOUNTS, (reply_id, None if amounts is None else Jsonb(amounts))
        )


@pytest.mark.parametrize(("role", "visible"), [("admin", True), ("sales", False)])
def test_every_admin_reads_staff_amounts_and_sales_do_not(
    db_conn: psycopg.Connection[Any],
    agent_database_url: str,
    sign_in_as: Callable[[str], None],
    role: str,
    visible: bool,
) -> None:
    """A stated amount is a price the customer was told, not a cost figure:
    readable by an admin without cost visibility (0019's allow-list)."""
    reply_id, _, _ = _claimed_reply(db_conn)
    with psycopg.connect(agent_database_url, autocommit=True) as agent:
        agent.execute(_RECORD_AMOUNTS, (reply_id, Jsonb(_AMOUNTS)))
    reader = _seed_user(db_conn, role=role, can_view_cost=False)

    sign_in_as(reader)
    rows = db_conn.execute("SELECT row_id FROM audit_log").fetchall()

    assert rows == ([(str(reply_id),)] if visible else [])


# ---------------------------------------------------------------------------
# messages.staff_reply_id
# ---------------------------------------------------------------------------


def test_the_agent_records_the_sent_reply_as_a_linked_outbound_message(
    db_conn: psycopg.Connection[Any], agent_database_url: str
) -> None:
    conversation_id, takeover_id, _ = _taken_over(db_conn)
    reply_id = seed_staff_reply(db_conn, takeover_id)

    with psycopg.connect(agent_database_url, autocommit=True) as agent:
        agent.execute(
            "INSERT INTO messages (conversation_id, customer_phone, direction, "
            "whatsapp_message_id, body, staff_reply_id) "
            "VALUES (%s, %s, 'outbound', 'wamid.OUT-1', 'Hello', %s)",
            (conversation_id, _PHONE, reply_id),
        )

    row = db_conn.execute(
        "SELECT staff_reply_id FROM messages WHERE whatsapp_message_id = 'wamid.OUT-1'"
    ).fetchone()
    assert row == (reply_id,)


def test_a_reply_links_to_one_outbound_message_only(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, takeover_id, _ = _taken_over(db_conn)
    reply_id = seed_staff_reply(db_conn, takeover_id)
    insert = (
        "INSERT INTO messages (conversation_id, customer_phone, direction, body, "
        "staff_reply_id) VALUES (%s, %s, %s, 'Hello', %s)"
    )

    with pytest.raises(psycopg.errors.CheckViolation):
        db_conn.execute(insert, (conversation_id, _PHONE, "inbound", reply_id))
    db_conn.execute(insert, (conversation_id, _PHONE, "outbound", reply_id))
    with pytest.raises(psycopg.errors.UniqueViolation):
        db_conn.execute(insert, (conversation_id, _PHONE, "outbound", reply_id))


# ---------------------------------------------------------------------------
# Erasure, rule 11
# ---------------------------------------------------------------------------


def test_erasing_a_customer_removes_their_staff_replies_and_messages(
    db_conn: psycopg.Connection[Any],
) -> None:
    conversation_id, takeover_id, _ = _taken_over(db_conn)
    reply_id = seed_staff_reply(db_conn, takeover_id)
    seed_message(db_conn, conversation_id, direction="inbound", body="hi")
    db_conn.execute(
        "INSERT INTO messages (conversation_id, customer_phone, direction, body, "
        "staff_reply_id) VALUES (%s, %s, 'outbound', 'Hello', %s)",
        (conversation_id, _PHONE, reply_id),
    )
    _, other_takeover, _ = _taken_over(db_conn, phone=_OTHER_PHONE)
    other_reply = seed_staff_reply(db_conn, other_takeover)

    db_conn.execute("SET SESSION AUTHORIZATION service_role")
    try:
        db_conn.execute("SELECT conversations_erase_customer(%s)", (_PHONE,))
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")

    assert db_conn.execute("SELECT id FROM staff_replies").fetchall() == [
        (other_reply,)
    ]
    assert db_conn.execute("SELECT count(*) FROM messages").fetchone() == (0,)


@pytest.mark.parametrize("role", ["authenticated", "hotel_agent"])
def test_every_write_policy_on_staff_replies_has_a_paired_select_policy(
    db_conn: psycopg.Connection[Any], role: str
) -> None:
    """CLAUDE.md rule 11."""
    rows = db_conn.execute(
        "SELECT cmd FROM pg_policies WHERE schemaname = 'public' "
        "AND tablename = 'staff_replies' AND %s = ANY (roles)",
        (role,),
    ).fetchall()
    commands = {row[0] for row in rows}

    assert commands & {"INSERT", "UPDATE"}
    assert "SELECT" in commands
    assert not commands & {"ALL", "DELETE"}
