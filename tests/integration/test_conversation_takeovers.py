"""Migration 0034 against a real Postgres: taking a conversation over,
resolving it and handing it back (staff notification step 2a, owner
decisions 2026-10-02) -- who may do each, the one-winner guarantee under
real concurrent transactions, the immutable history, what the agent role
may read and write, and erasure."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from psycopg import sql

from tests.integration._seed import seed_conversation, seed_escalation

pytestmark = pytest.mark.usefixtures("db_conn")

_PHONE = "+966500000001"
_OTHER_PHONE = "+966500000002"
_THREAD_JOIN_TIMEOUT_S = 10
# How long the first takeover's transaction stays open while the second
# one waits on it: long enough that the second is certainly blocked.
_HOLD_OPEN_S = 0.5

_TAKE_OVER = "SELECT * FROM staff_take_over_conversation(%s)"
_CLOSE = "SELECT staff_close_conversation(%s, %s)"


def _seed_user(
    conn: psycopg.Connection[Any],
    *,
    role: str | None,
    is_active: bool = True,
    full_name: str = "Test User",
) -> str:
    """An auth.users identity, with an app_users row in `role` unless role
    is None (signed in, never provisioned)."""
    row = conn.execute("INSERT INTO auth.users DEFAULT VALUES RETURNING id").fetchone()
    assert row is not None
    user_id = str(row[0])
    if role is not None:
        conn.execute(
            "INSERT INTO app_users (id, full_name, app_role, is_active) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, full_name, role, is_active),
        )
    return user_id


def _escalated_conversation(conn: psycopg.Connection[Any], phone: str = _PHONE) -> int:
    conversation_id = seed_conversation(conn, customer_phone=phone)
    seed_escalation(
        conn, conversation_id, reason="booking_requested", customer_phone=phone
    )
    return conversation_id


def _reset(conn: psycopg.Connection[Any]) -> None:
    conn.execute("RESET SESSION AUTHORIZATION")
    conn.execute("RESET request.jwt.claim.sub")


@contextmanager
def _signed_in(dsn: str, user_id: str) -> Iterator[psycopg.Connection[Any]]:
    """A connection of its own as `user_id`, for a second actor running
    concurrently with the first."""
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        conn.execute("SET SESSION AUTHORIZATION authenticated")
        conn.execute(
            sql.SQL("SET request.jwt.claim.sub = {}").format(sql.Literal(user_id))
        )
        yield conn
    finally:
        conn.close()


def _takeovers(conn: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT conversation_id, taken_over_by::text, ended_by::text, outcome, "
        "ended_at IS NOT NULL FROM conversation_takeovers ORDER BY id"
    ).fetchall()


def _open_escalation_count(conn: psycopg.Connection[Any], conversation_id: int) -> int:
    row = conn.execute(
        "SELECT count(*) FROM escalations "
        "WHERE conversation_id = %s AND resolved_at IS NULL",
        (conversation_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# Taking over
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "sales"])
def test_staff_take_over_a_conversation_with_an_open_escalation(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None], role: str
) -> None:
    conversation_id = _escalated_conversation(db_conn)
    user_id = _seed_user(db_conn, role=role)

    sign_in_as(user_id)
    row = db_conn.execute(_TAKE_OVER, (conversation_id,)).fetchone()
    _reset(db_conn)

    assert row is not None
    takeover_id, won, holder_id, holder_since = row
    assert (won, str(holder_id)) == (True, user_id)
    assert holder_since is not None
    assert _takeovers(db_conn) == [(conversation_id, user_id, None, None, False)]
    stored = db_conn.execute(
        "SELECT id FROM conversation_takeovers WHERE ended_at IS NULL"
    ).fetchone()
    assert stored == (takeover_id,)


def test_the_second_staff_member_loses_and_is_told_who_holds_it(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    conversation_id = _escalated_conversation(db_conn)
    first = _seed_user(db_conn, role="sales")
    second = _seed_user(db_conn, role="admin")

    sign_in_as(first)
    first_row = db_conn.execute(_TAKE_OVER, (conversation_id,)).fetchone()
    sign_in_as(second)
    second_row = db_conn.execute(_TAKE_OVER, (conversation_id,)).fetchone()
    _reset(db_conn)

    assert first_row is not None
    assert second_row is not None
    assert second_row[0] == first_row[0]
    assert (second_row[1], str(second_row[2])) == (False, first)
    assert len(_takeovers(db_conn)) == 1


def test_concurrent_takeovers_have_exactly_one_winner(
    db_conn: psycopg.Connection[Any], test_database_url: str
) -> None:
    """Real concurrent transactions, no mocks: the second takeover runs
    while the first is inserted but not yet committed, so it must wait on
    the partial unique index and then lose to the committed row."""
    conversation_id = _escalated_conversation(db_conn)
    first = _seed_user(db_conn, role="sales")
    second = _seed_user(db_conn, role="sales")
    first_inserted = threading.Event()
    outcomes: dict[str, Any] = {}

    def take_over_and_hold_the_transaction_open() -> None:
        with _signed_in(test_database_url, first) as conn, conn.transaction():
            outcomes["first"] = conn.execute(_TAKE_OVER, (conversation_id,)).fetchone()
            first_inserted.set()
            time.sleep(_HOLD_OPEN_S)

    def take_over_while_the_first_is_uncommitted() -> None:
        first_inserted.wait(timeout=_THREAD_JOIN_TIMEOUT_S)
        with _signed_in(test_database_url, second) as conn:
            started = time.monotonic()
            outcomes["second"] = conn.execute(_TAKE_OVER, (conversation_id,)).fetchone()
            outcomes["second_waited_s"] = time.monotonic() - started

    threads = [
        threading.Thread(target=take_over_and_hold_the_transaction_open),
        threading.Thread(target=take_over_while_the_first_is_uncommitted),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=_THREAD_JOIN_TIMEOUT_S)
    assert not any(thread.is_alive() for thread in threads), f"deadlock: {outcomes}"

    assert outcomes["first"][1] is True
    assert outcomes["second"][1] is False
    assert str(outcomes["second"][2]) == first
    assert outcomes["second"][0] == outcomes["first"][0]
    # It really waited on the uncommitted row rather than running after it.
    assert outcomes["second_waited_s"] >= _HOLD_OPEN_S / 2
    assert [row[1] for row in _takeovers(db_conn)] == [first]


def test_no_takeover_of_a_conversation_without_an_open_escalation(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    conversation_id = _escalated_conversation(db_conn)
    db_conn.execute("UPDATE escalations SET resolved_at = now()")
    user_id = _seed_user(db_conn, role="admin")

    sign_in_as(user_id)
    with pytest.raises(psycopg.errors.NoDataFound):
        db_conn.execute(_TAKE_OVER, (conversation_id,))
    # Nor by writing the table directly: the policy refuses the row.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute(
            "INSERT INTO conversation_takeovers (conversation_id, taken_over_by) "
            "VALUES (%s, %s)",
            (conversation_id, user_id),
        )


@pytest.mark.parametrize(
    ("role", "is_active"),
    [
        pytest.param("sales", False, id="deactivated"),
        pytest.param(None, True, id="never-provisioned"),
    ],
)
def test_anyone_who_is_not_active_staff_cannot_take_over(
    db_conn: psycopg.Connection[Any],
    sign_in_as: Callable[[str], None],
    role: str | None,
    is_active: bool,
) -> None:
    conversation_id = _escalated_conversation(db_conn)
    user_id = _seed_user(db_conn, role=role, is_active=is_active)

    sign_in_as(user_id)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute(_TAKE_OVER, (conversation_id,))


def test_nobody_takes_over_in_someone_elses_name(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    conversation_id = _escalated_conversation(db_conn)
    user_id = _seed_user(db_conn, role="admin")
    other_id = _seed_user(db_conn, role="sales")

    sign_in_as(user_id)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute(
            "INSERT INTO conversation_takeovers (conversation_id, taken_over_by) "
            "VALUES (%s, %s)",
            (conversation_id, other_id),
        )


def test_the_takeover_time_cannot_be_chosen_by_the_client(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    conversation_id = _escalated_conversation(db_conn)
    user_id = _seed_user(db_conn, role="admin")

    sign_in_as(user_id)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute(
            "INSERT INTO conversation_takeovers "
            "(conversation_id, taken_over_by, taken_over_at) "
            "VALUES (%s, %s, now() - interval '1 hour')",
            (conversation_id, user_id),
        )


# ---------------------------------------------------------------------------
# Resolving and handing back
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["resolved", "handed_back"])
def test_the_holder_closes_the_conversation_and_every_open_escalation(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None], outcome: str
) -> None:
    conversation_id = _escalated_conversation(db_conn)
    seed_escalation(db_conn, conversation_id, reason="delivery_failed")
    already_closed = seed_escalation(db_conn, conversation_id, reason="empty_reply")
    db_conn.execute(
        "UPDATE escalations SET resolved_at = opened_at WHERE id = %s",
        (already_closed,),
    )
    user_id = _seed_user(db_conn, role="sales")

    sign_in_as(user_id)
    db_conn.execute(_TAKE_OVER, (conversation_id,))
    closed = db_conn.execute(_CLOSE, (conversation_id, outcome)).fetchone()
    _reset(db_conn)

    assert closed == (2,)
    assert _takeovers(db_conn) == [(conversation_id, user_id, user_id, outcome, True)]
    assert _open_escalation_count(db_conn, conversation_id) == 0
    untouched = db_conn.execute(
        "SELECT resolved_at = opened_at FROM escalations WHERE id = %s",
        (already_closed,),
    ).fetchone()
    assert untouched == (True,)


def test_resolve_also_closes_a_conversation_nobody_took_over(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """Owner decision D2: escalations that need no action (a voice note)
    can be closed without taking the customer over."""
    conversation_id = _escalated_conversation(db_conn)
    user_id = _seed_user(db_conn, role="sales")

    sign_in_as(user_id)
    closed = db_conn.execute(_CLOSE, (conversation_id, "resolved")).fetchone()
    _reset(db_conn)

    assert closed == (1,)
    assert _takeovers(db_conn) == []
    assert _open_escalation_count(db_conn, conversation_id) == 0


def test_hand_back_needs_an_active_takeover(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    conversation_id = _escalated_conversation(db_conn)
    user_id = _seed_user(db_conn, role="sales")

    sign_in_as(user_id)
    with pytest.raises(psycopg.errors.NoDataFound):
        db_conn.execute(_CLOSE, (conversation_id, "handed_back"))
    _reset(db_conn)

    assert _open_escalation_count(db_conn, conversation_id) == 1


def test_an_unknown_outcome_is_refused(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    conversation_id = _escalated_conversation(db_conn)
    user_id = _seed_user(db_conn, role="admin")

    sign_in_as(user_id)
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        db_conn.execute(_CLOSE, (conversation_id, "abandoned"))


@pytest.mark.parametrize("outcome", ["resolved", "handed_back"])
def test_another_sales_user_cannot_close_someone_elses_takeover(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None], outcome: str
) -> None:
    conversation_id = _escalated_conversation(db_conn)
    holder = _seed_user(db_conn, role="sales")
    other = _seed_user(db_conn, role="sales")
    sign_in_as(holder)
    db_conn.execute(_TAKE_OVER, (conversation_id,))

    sign_in_as(other)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute(_CLOSE, (conversation_id, outcome))
    # Nor directly: neither row is theirs to update.
    ended = db_conn.execute(
        "UPDATE conversation_takeovers SET ended_at = now(), ended_by = %s, "
        "outcome = 'resolved' WHERE ended_at IS NULL",
        (other,),
    )
    resolved = db_conn.execute("UPDATE escalations SET resolved_at = now()")
    _reset(db_conn)

    assert (ended.rowcount, resolved.rowcount) == (0, 0)
    assert _takeovers(db_conn) == [(conversation_id, holder, None, None, False)]
    assert _open_escalation_count(db_conn, conversation_id) == 1


def test_an_admin_can_close_someone_elses_takeover(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """Owner decision D4: a holder who went off shift or was deactivated
    must not leave the bot silent for good."""
    conversation_id = _escalated_conversation(db_conn)
    holder = _seed_user(db_conn, role="sales")
    admin = _seed_user(db_conn, role="admin")
    sign_in_as(holder)
    db_conn.execute(_TAKE_OVER, (conversation_id,))
    _reset(db_conn)
    db_conn.execute("UPDATE app_users SET is_active = false WHERE id = %s", (holder,))

    sign_in_as(admin)
    closed = db_conn.execute(_CLOSE, (conversation_id, "handed_back")).fetchone()
    _reset(db_conn)

    assert closed == (1,)
    assert _takeovers(db_conn) == [
        (conversation_id, holder, admin, "handed_back", True)
    ]


def test_an_ended_takeover_can_never_change_again(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    conversation_id = _escalated_conversation(db_conn)
    admin = _seed_user(db_conn, role="admin")
    sign_in_as(admin)
    db_conn.execute(_TAKE_OVER, (conversation_id,))
    db_conn.execute(_CLOSE, (conversation_id, "resolved"))

    changed = db_conn.execute(
        "UPDATE conversation_takeovers SET ended_at = now(), ended_by = %s, "
        "outcome = 'handed_back'",
        (admin,),
    )
    _reset(db_conn)

    assert changed.rowcount == 0
    assert _takeovers(db_conn) == [(conversation_id, admin, admin, "resolved", True)]


# A time other than the statement's own, but still after the takeover, so
# only the policy -- not the table's CHECK constraints -- can refuse it.
@pytest.mark.parametrize(
    "statement",
    [
        pytest.param(
            "UPDATE conversation_takeovers SET outcome = 'resolved', "
            "ended_at = now() + interval '1 hour', ended_by = {me}",
            id="another-time",
        ),
        pytest.param(
            "UPDATE conversation_takeovers SET outcome = 'resolved', "
            "ended_at = now(), ended_by = {other}",
            id="someone-else",
        ),
    ],
)
def test_an_end_cannot_be_given_another_time_or_someone_elses_name(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None], statement: str
) -> None:
    conversation_id = _escalated_conversation(db_conn)
    holder = _seed_user(db_conn, role="admin")
    other = _seed_user(db_conn, role="sales")
    sign_in_as(holder)
    db_conn.execute(_TAKE_OVER, (conversation_id,))

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute(
            sql.SQL(statement).format(me=sql.Literal(holder), other=sql.Literal(other))
        )


def test_an_escalation_is_closed_at_the_statements_own_time_only(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    _escalated_conversation(db_conn)
    user_id = _seed_user(db_conn, role="admin")

    sign_in_as(user_id)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute("UPDATE escalations SET resolved_at = now() + interval '1 day'")


def test_staff_cannot_delete_takeovers(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    conversation_id = _escalated_conversation(db_conn)
    admin = _seed_user(db_conn, role="admin")
    sign_in_as(admin)
    db_conn.execute(_TAKE_OVER, (conversation_id,))

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_conn.execute("DELETE FROM conversation_takeovers")


def test_each_takeover_stays_in_the_history(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    conversation_id = _escalated_conversation(db_conn)
    first = _seed_user(db_conn, role="sales")
    second = _seed_user(db_conn, role="sales")
    sign_in_as(first)
    db_conn.execute(_TAKE_OVER, (conversation_id,))
    db_conn.execute(_CLOSE, (conversation_id, "handed_back"))
    _reset(db_conn)
    seed_escalation(db_conn, conversation_id, reason="delivery_failed")

    sign_in_as(second)
    row = db_conn.execute(_TAKE_OVER, (conversation_id,)).fetchone()
    _reset(db_conn)

    assert row is not None
    assert row[1] is True
    assert _takeovers(db_conn) == [
        (conversation_id, first, first, "handed_back", True),
        (conversation_id, second, None, None, False),
    ]


@pytest.mark.parametrize(
    "query",
    [
        "SELECT id FROM conversation_takeovers",
        "SELECT id FROM staff_names_for_dashboard",
    ],
)
def test_inactive_users_read_nothing(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None], query: str
) -> None:
    conversation_id = _escalated_conversation(db_conn)
    admin = _seed_user(db_conn, role="admin")
    sign_in_as(admin)
    db_conn.execute(_TAKE_OVER, (conversation_id,))
    _reset(db_conn)
    deactivated = _seed_user(db_conn, role="sales", is_active=False)

    sign_in_as(deactivated)

    assert db_conn.execute(query).fetchall() == []


# ---------------------------------------------------------------------------
# Staff names (owner decision D7)
# ---------------------------------------------------------------------------


def test_every_active_staff_member_reads_every_staff_name(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    """Deactivated staff are listed too: they stay in the history."""
    reader = _seed_user(db_conn, role="sales", full_name="Reader")
    admin = _seed_user(db_conn, role="admin", full_name="Admin")
    gone = _seed_user(db_conn, role="sales", is_active=False, full_name="Gone")

    sign_in_as(reader)
    names = db_conn.execute(
        "SELECT id::text, full_name FROM staff_names_for_dashboard ORDER BY full_name"
    ).fetchall()

    assert names == [(admin, "Admin"), (gone, "Gone"), (reader, "Reader")]


def test_the_names_view_shows_no_role_or_cost_visibility(
    db_conn: psycopg.Connection[Any], sign_in_as: Callable[[str], None]
) -> None:
    reader = _seed_user(db_conn, role="sales")

    sign_in_as(reader)
    with pytest.raises(psycopg.errors.UndefinedColumn):
        db_conn.execute("SELECT app_role FROM staff_names_for_dashboard")


def test_anon_reads_no_staff_names(db_conn: psycopg.Connection[Any]) -> None:
    _seed_user(db_conn, role="admin")

    db_conn.execute("SET SESSION AUTHORIZATION anon")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_conn.execute("SELECT id FROM staff_names_for_dashboard")
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")


# ---------------------------------------------------------------------------
# The agent role (hotel_agent)
# ---------------------------------------------------------------------------


def _taken_over_by_admin(db_conn: psycopg.Connection[Any]) -> tuple[int, int, str]:
    """An escalated conversation an admin has taken over; returns the
    conversation id, the takeover id and the admin's id."""
    conversation_id = _escalated_conversation(db_conn)
    admin = _seed_user(db_conn, role="admin")
    db_conn.execute("SET SESSION AUTHORIZATION authenticated")
    db_conn.execute(
        sql.SQL("SET request.jwt.claim.sub = {}").format(sql.Literal(admin))
    )
    row = db_conn.execute(_TAKE_OVER, (conversation_id,)).fetchone()
    _reset(db_conn)
    assert row is not None
    return conversation_id, int(row[0]), admin


def test_the_agent_reads_whether_a_conversation_is_taken_over(
    db_conn: psycopg.Connection[Any], agent_database_url: str
) -> None:
    conversation_id, takeover_id, _ = _taken_over_by_admin(db_conn)

    with psycopg.connect(agent_database_url, autocommit=True) as agent:
        row = agent.execute(
            "SELECT id, conversation_id, ended_at, ack_claimed_at, ack_sent_at, "
            "ack_failed_at FROM conversation_takeovers"
        ).fetchone()

    assert row == (takeover_id, conversation_id, None, None, None, None)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT taken_over_by FROM conversation_takeovers",
        "SELECT ended_by FROM conversation_takeovers",
        "SELECT outcome FROM conversation_takeovers",
        "UPDATE conversation_takeovers SET ended_at = now()",
        "INSERT INTO conversation_takeovers (conversation_id) VALUES (1)",
        "DELETE FROM conversation_takeovers",
        "SELECT * FROM staff_names_for_dashboard",
        "SELECT * FROM staff_take_over_conversation(1)",
        "SELECT staff_close_conversation(1, 'resolved')",
    ],
)
def test_the_agent_never_sees_who_holds_it_and_never_takes_over_or_ends(
    agent_database_url: str, statement: str
) -> None:
    with (
        psycopg.connect(agent_database_url, autocommit=True) as agent,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        agent.execute(statement)


def test_the_agent_claims_the_acknowledgement_once_then_records_it(
    db_conn: psycopg.Connection[Any], agent_database_url: str
) -> None:
    _, takeover_id, _ = _taken_over_by_admin(db_conn)
    claim = (
        "UPDATE conversation_takeovers SET ack_claimed_at = now() "
        "WHERE id = %s AND ended_at IS NULL AND ack_claimed_at IS NULL"
    )

    with psycopg.connect(agent_database_url, autocommit=True) as agent:
        first = agent.execute(claim, (takeover_id,)).rowcount
        second = agent.execute(claim, (takeover_id,)).rowcount
        recorded = agent.execute(
            "UPDATE conversation_takeovers SET ack_sent_at = now() WHERE id = %s",
            (takeover_id,),
        ).rowcount
        again = agent.execute(
            "UPDATE conversation_takeovers SET ack_failed_at = now() WHERE id = %s",
            (takeover_id,),
        ).rowcount

    assert (first, second, recorded, again) == (1, 0, 1, 0)


def test_the_agent_cannot_claim_the_acknowledgement_of_an_ended_takeover(
    db_conn: psycopg.Connection[Any], agent_database_url: str
) -> None:
    conversation_id, takeover_id, _ = _taken_over_by_admin(db_conn)
    db_conn.execute(
        "UPDATE conversation_takeovers SET ended_at = now(), ended_by = taken_over_by, "
        "outcome = 'resolved' WHERE conversation_id = %s",
        (conversation_id,),
    )

    with psycopg.connect(agent_database_url, autocommit=True) as agent:
        claimed = agent.execute(
            "UPDATE conversation_takeovers SET ack_claimed_at = now() WHERE id = %s",
            (takeover_id,),
        ).rowcount

    assert claimed == 0


def test_the_agent_records_the_outcome_even_if_the_takeover_ended_meanwhile(
    db_conn: psycopg.Connection[Any], agent_database_url: str
) -> None:
    conversation_id, takeover_id, _ = _taken_over_by_admin(db_conn)

    with psycopg.connect(agent_database_url, autocommit=True) as agent:
        agent.execute(
            "UPDATE conversation_takeovers SET ack_claimed_at = now() WHERE id = %s",
            (takeover_id,),
        )
        db_conn.execute(
            "UPDATE conversation_takeovers SET ended_at = now(), "
            "ended_by = taken_over_by, outcome = 'resolved' WHERE conversation_id = %s",
            (conversation_id,),
        )
        recorded = agent.execute(
            "UPDATE conversation_takeovers SET ack_sent_at = now() WHERE id = %s",
            (takeover_id,),
        ).rowcount

    assert recorded == 1


# ---------------------------------------------------------------------------
# Erasure, rule 11, live updates
# ---------------------------------------------------------------------------


def test_erasing_a_customer_removes_their_takeovers(
    db_conn: psycopg.Connection[Any],
) -> None:
    _taken_over_by_admin(db_conn)
    other_conversation = _escalated_conversation(db_conn, phone=_OTHER_PHONE)

    db_conn.execute("SET SESSION AUTHORIZATION service_role")
    try:
        db_conn.execute("SELECT conversations_erase_customer(%s)", (_PHONE,))
    finally:
        db_conn.execute("RESET SESSION AUTHORIZATION")

    assert _takeovers(db_conn) == []
    remaining = db_conn.execute("SELECT id FROM conversations").fetchall()
    assert remaining == [(other_conversation,)]


@pytest.mark.parametrize("table", ["conversation_takeovers", "escalations"])
def test_every_staff_write_policy_has_a_paired_select_policy(
    db_conn: psycopg.Connection[Any], table: str
) -> None:
    """CLAUDE.md rule 11, for the dashboard's role on the two tables this
    migration lets it write."""
    rows = db_conn.execute(
        "SELECT cmd FROM pg_policies WHERE schemaname = 'public' "
        "AND tablename = %s AND 'authenticated' = ANY (roles)",
        (table,),
    ).fetchall()
    commands = {row[0] for row in rows}

    assert commands & {"INSERT", "UPDATE"}
    assert "SELECT" in commands
    assert not commands & {"ALL", "DELETE"}
