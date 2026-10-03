"""The staff reply endpoint end to end, through the real app against a real
Postgres (staff notification step 3, owner decisions 2026-10-02): a reply
is claimed once, its amounts are audited in its author's name before it is
sent, and it is recorded as an outbound message linked to it.

Every test runs as the privileged test role and as hotel_agent, the role
the agent connects as (the wiring fixture). The WhatsApp sender is a fake;
the output guard and the database are real.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from services.agent import staff_reply as staff_reply_module
from services.agent import takeover_ack as takeover_ack_module
from services.agent import webhook as webhook_module
from services.agent.main import app
from services.agent.takeover_ack import InternalApiSettings
from services.agent.whatsapp_send import (
    ReplyButton,
    WhatsAppSendError,
    WhatsAppSendSettings,
)
from tests.integration._seed import (
    seed_conversation,
    seed_escalation,
    seed_hotel,
    seed_message,
    seed_staff_reply,
    seed_staff_template_reply,
    seed_takeover,
)

pytestmark = pytest.mark.usefixtures("db_conn")

_INTERNAL_TOKEN = "test-agent-internal-token-placeholder"
_PHONE = "+966500000001"
_WA_ID = "966500000001"
_TEST_WHATSAPP_SETTINGS = WhatsAppSendSettings(
    phone_number_id="test-phone-number-id",
    access_token="test-access-token",
    timeout_ms=10_000,
)
_PRICED_REPLY = "Your room is 1,500 ريال for both nights"


@dataclass
class _RecordingSender:
    """Records every send, with a distinct message id per success; fails
    every send if told to."""

    fail: bool = False
    calls: list[tuple[str, str]] = field(default_factory=list)
    template_calls: list[tuple[str, str, str, tuple[str, ...]]] = field(
        default_factory=list
    )

    async def send_text(self, *, to_phone: str, body: str) -> str:
        self.calls.append((to_phone, body))
        if self.fail:
            raise WhatsAppSendError("simulated API error")
        return f"wamid.OUT-{len(self.calls)}"

    async def send_template(
        self,
        *,
        to_phone: str,
        template_name: str,
        language_code: str,
        body_parameters: tuple[str, ...],
    ) -> str:
        self.template_calls.append(
            (to_phone, template_name, language_code, body_parameters)
        )
        if self.fail:
            raise WhatsAppSendError("simulated API error")
        return f"wamid.TEMPLATE-{len(self.template_calls)}"

    async def send_reply_buttons(
        self, *, to_phone: str, body: str, buttons: tuple[ReplyButton, ...]
    ) -> str:
        del buttons
        return await self.send_text(to_phone=to_phone, body=body)


@contextlib.contextmanager
def _nullcontext(conn: psycopg.Connection[Any]) -> Iterator[psycopg.Connection[Any]]:
    yield conn


@contextlib.contextmanager
def _agent_connection(dsn: str) -> Iterator[psycopg.Connection[Any]]:
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


@dataclass
class _Wiring:
    client: TestClient
    sender: _RecordingSender


@pytest.fixture(params=["privileged", "hotel_agent"])
def wiring(
    request: pytest.FixtureRequest,
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> _Wiring:
    """The real app with a fixed internal token and a recording sender, on
    the test's own connection or on a fresh one as hotel_agent."""
    if request.param == "hotel_agent":
        agent_url: str = request.getfixturevalue("agent_database_url")
        monkeypatch.setattr(
            webhook_module, "get_db_connection", lambda: _agent_connection(agent_url)
        )
    else:
        monkeypatch.setattr(
            webhook_module, "get_db_connection", lambda: _nullcontext(db_conn)
        )
    monkeypatch.setattr(
        takeover_ack_module,
        "get_internal_api_settings",
        lambda: InternalApiSettings(token=_INTERNAL_TOKEN),
    )
    monkeypatch.setattr(
        webhook_module, "get_whatsapp_send_settings", lambda: _TEST_WHATSAPP_SETTINGS
    )
    sender = _RecordingSender()
    monkeypatch.setattr(webhook_module, "get_whatsapp_sender", lambda _s: sender)
    return _Wiring(client=TestClient(app), sender=sender)


def _send(client: TestClient, staff_reply_id: int) -> Any:
    return client.post(
        f"/internal/staff-replies/{staff_reply_id}/send",
        headers={"Authorization": f"Bearer {_INTERNAL_TOKEN}"},
    )


def _taken_over_conversation(
    db_conn: psycopg.Connection[Any], *, customer_wrote_ago: timedelta | None = None
) -> tuple[int, int]:
    """A customer with an open escalation and an active takeover, who last
    wrote `customer_wrote_ago` ago (5 minutes unless given); returns the
    conversation and takeover ids."""
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    seed_escalation(db_conn, conversation_id, reason="booking_requested")
    ago = customer_wrote_ago or timedelta(minutes=5)
    seed_message(
        db_conn,
        conversation_id,
        direction="inbound",
        body="Is there a room?",
        customer_phone=_PHONE,
        created_at=datetime.now(UTC) - ago,
    )
    return conversation_id, seed_takeover(db_conn, conversation_id)


def _reply_state(db_conn: psycopg.Connection[Any], reply_id: int) -> tuple[Any, ...]:
    row = db_conn.execute(
        "SELECT claimed_at IS NOT NULL, sent_at IS NOT NULL, failed_at IS NOT NULL, "
        "failure_reason FROM staff_replies WHERE id = %s",
        (reply_id,),
    ).fetchone()
    assert row is not None
    return tuple(row)


def _outbound(db_conn: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return db_conn.execute(
        "SELECT body, staff_reply_id FROM messages "
        "WHERE direction = 'outbound' ORDER BY id"
    ).fetchall()


def _audited(db_conn: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return db_conn.execute(
        "SELECT row_id, new_value, changed_by::text FROM audit_log "
        "WHERE table_name = 'staff_replies' ORDER BY id"
    ).fetchall()


def _author(db_conn: psycopg.Connection[Any], reply_id: int) -> str:
    row = db_conn.execute(
        "SELECT sent_by::text FROM staff_replies WHERE id = %s", (reply_id,)
    ).fetchone()
    assert row is not None
    author: str = row[0]
    return author


def test_a_reply_is_sent_once_and_recorded_as_a_linked_message(
    wiring: _Wiring, db_conn: psycopg.Connection[Any]
) -> None:
    _, takeover_id = _taken_over_conversation(db_conn)
    reply_id = seed_staff_reply(db_conn, takeover_id, body="Your room is ready")

    first = _send(wiring.client, reply_id)
    second = _send(wiring.client, reply_id)

    assert (first.status_code, first.json()) == (200, {"status": "sent"})
    assert (second.status_code, second.json()) == (200, {"status": "already_claimed"})
    assert wiring.sender.calls == [(_WA_ID, "Your room is ready")]
    assert _outbound(db_conn) == [("Your room is ready", reply_id)]
    assert _reply_state(db_conn, reply_id) == (True, True, False, None)
    assert _audited(db_conn) == []


def test_a_stated_amount_is_audited_in_the_authors_name_and_still_sent(
    wiring: _Wiring, db_conn: psycopg.Connection[Any]
) -> None:
    """Owner decision: the guard's price check does not apply to staff,
    but every amount they state is written to audit_log."""
    conversation_id, takeover_id = _taken_over_conversation(db_conn)
    reply_id = seed_staff_reply(db_conn, takeover_id, body=_PRICED_REPLY)

    response = _send(wiring.client, reply_id)

    assert response.json() == {"status": "sent"}
    assert wiring.sender.calls == [(_WA_ID, _PRICED_REPLY)]
    assert _audited(db_conn) == [
        (
            str(reply_id),
            {
                "conversation_id": conversation_id,
                "amounts": [
                    {
                        "raw": "1,500",
                        "halalas": 150_000,
                        "sar_marker": True,
                        "foreign_currency_marker": None,
                        "percentage": False,
                    }
                ],
            },
            _author(db_conn, reply_id),
        )
    ]


def test_a_reply_whose_takeover_ended_is_not_sent(
    wiring: _Wiring, db_conn: psycopg.Connection[Any]
) -> None:
    _, takeover_id = _taken_over_conversation(db_conn)
    reply_id = seed_staff_reply(db_conn, takeover_id)
    db_conn.execute(
        "UPDATE conversation_takeovers SET ended_at = now(), ended_by = taken_over_by, "
        "outcome = 'handed_back' WHERE id = %s",
        (takeover_id,),
    )

    response = _send(wiring.client, reply_id)

    assert (response.status_code, response.json()) == (
        200,
        {"status": "takeover_ended"},
    )
    assert wiring.sender.calls == []
    assert _reply_state(db_conn, reply_id) == (False, False, False, None)


def test_an_unknown_reply_is_not_found(wiring: _Wiring) -> None:
    response = _send(wiring.client, 999_999)

    assert (response.status_code, response.json()) == (404, {"status": "not_found"})
    assert wiring.sender.calls == []


def test_outside_the_24_hour_window_nothing_is_sent_but_amounts_are_audited(
    wiring: _Wiring, db_conn: psycopg.Connection[Any]
) -> None:
    """Meta refuses a free-form message there; the dashboard offers the
    re-engagement template instead. The amounts are audited at the claim,
    whatever happens to the send."""
    _, takeover_id = _taken_over_conversation(
        db_conn, customer_wrote_ago=timedelta(hours=25)
    )
    reply_id = seed_staff_reply(db_conn, takeover_id, body=_PRICED_REPLY)

    response = _send(wiring.client, reply_id)

    assert (response.status_code, response.json()) == (
        200,
        {"status": "outside_window"},
    )
    assert wiring.sender.calls == []
    assert _outbound(db_conn) == []
    assert _reply_state(db_conn, reply_id) == (True, False, True, "outside_window")
    assert len(_audited(db_conn)) == 1


def test_a_failed_send_is_recorded_and_never_retried(
    wiring: _Wiring, db_conn: psycopg.Connection[Any]
) -> None:
    _, takeover_id = _taken_over_conversation(db_conn)
    reply_id = seed_staff_reply(db_conn, takeover_id)
    wiring.sender.fail = True

    first = _send(wiring.client, reply_id)
    second = _send(wiring.client, reply_id)

    assert (first.status_code, first.json()) == (502, {"status": "failed"})
    assert second.json() == {"status": "already_claimed"}
    assert len(wiring.sender.calls) == 1
    assert _outbound(db_conn) == []
    assert _reply_state(db_conn, reply_id) == (True, False, True, "send_failed")


def test_a_failed_audit_sends_nothing_and_leaves_the_reply_unclaimed(
    wiring: _Wiring,
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply is never sent without its audit row; the rollback leaves it
    for the dashboard to try again."""
    _, takeover_id = _taken_over_conversation(db_conn)
    reply_id = seed_staff_reply(db_conn, takeover_id, body=_PRICED_REPLY)

    def _failing_audit(*_args: Any, **_kwargs: Any) -> None:
        raise psycopg.errors.InsufficientPrivilege("simulated audit failure")

    monkeypatch.setattr(staff_reply_module, "record_stated_amounts", _failing_audit)

    response = _send(wiring.client, reply_id)

    assert (response.status_code, response.json()) == (503, {"status": "unavailable"})
    assert wiring.sender.calls == []
    assert _reply_state(db_conn, reply_id) == (False, False, False, None)


@pytest.mark.parametrize(
    "authorization",
    [
        pytest.param("", id="missing"),
        pytest.param(_INTERNAL_TOKEN, id="not-bearer"),
        pytest.param("Bearer not-the-token", id="wrong"),
    ],
)
def test_a_request_without_the_token_is_refused_before_any_database_access(
    wiring: _Wiring,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    authorization: str,
) -> None:
    def _no_database() -> Any:
        raise AssertionError("the token is checked before the database is opened")

    monkeypatch.setattr(webhook_module, "get_db_connection", _no_database)
    caplog.set_level(logging.WARNING, logger="services.agent.takeover_ack")
    headers = {"Authorization": authorization} if authorization else {}

    response = wiring.client.post("/internal/staff-replies/1/send", headers=headers)

    assert response.status_code == 401
    assert wiring.sender.calls == []
    logged = json.dumps([record.getMessage() for record in caplog.records])
    assert _INTERNAL_TOKEN not in logged


def test_an_unreachable_database_answers_unavailable(
    wiring: _Wiring, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _unreachable() -> Any:
        raise psycopg.OperationalError("simulated connection failure")

    monkeypatch.setattr(webhook_module, "get_db_connection", _unreachable)

    response = _send(wiring.client, 1)

    assert (response.status_code, response.json()) == (503, {"status": "unavailable"})


def test_the_reply_text_never_reaches_the_logs(
    wiring: _Wiring,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CLAUDE.md §8: no full customer-facing message text in the logs."""
    _, takeover_id = _taken_over_conversation(db_conn)
    reply_id = seed_staff_reply(db_conn, takeover_id, body=_PRICED_REPLY)
    caplog.set_level(logging.INFO)

    _send(wiring.client, reply_id)

    assert all(_PRICED_REPLY not in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# The re-engagement template (migration 0036; services/agent/
# reengagement_template.py): a reply of kind 'template', sent outside the
# 24-hour window only, and only once both template names are set.
# ---------------------------------------------------------------------------

_TEMPLATE_WITH_HOTEL = "reengagement_with_hotel"
_TEMPLATE_NO_HOTEL = "reengagement_no_hotel"
_OUTSIDE_WINDOW = timedelta(hours=25)


@pytest.fixture
def template_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHATSAPP_REENGAGEMENT_TEMPLATE", _TEMPLATE_WITH_HOTEL)
    monkeypatch.setenv("WHATSAPP_REENGAGEMENT_TEMPLATE_NO_HOTEL", _TEMPLATE_NO_HOTEL)


@pytest.fixture
def template_names_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WHATSAPP_REENGAGEMENT_TEMPLATE", raising=False)
    monkeypatch.delenv("WHATSAPP_REENGAGEMENT_TEMPLATE_NO_HOTEL", raising=False)


def _customer_wrote(
    db_conn: psycopg.Connection[Any], conversation_id: int, body: str, ago: timedelta
) -> None:
    seed_message(
        db_conn,
        conversation_id,
        direction="inbound",
        body=body,
        customer_phone=_PHONE,
        created_at=datetime.now(UTC) - ago,
    )


@pytest.mark.usefixtures("template_names")
def test_a_template_names_the_hotel_in_the_customers_language_and_is_recorded(
    wiring: _Wiring, db_conn: psycopg.Connection[Any]
) -> None:
    conversation_id, takeover_id = _taken_over_conversation(
        db_conn, customer_wrote_ago=_OUTSIDE_WINDOW
    )
    _customer_wrote(db_conn, conversation_id, "هل في غرفة متاحة؟", _OUTSIDE_WINDOW)
    hotel_id = seed_hotel(db_conn, hotel_name="  فندق\nزمزم  ")
    reply_id = seed_staff_template_reply(db_conn, takeover_id, hotel_id=hotel_id)

    first = _send(wiring.client, reply_id)
    second = _send(wiring.client, reply_id)

    assert (first.status_code, first.json()) == (200, {"status": "sent"})
    assert second.json() == {"status": "already_claimed"}
    assert wiring.sender.calls == []
    assert wiring.sender.template_calls == [
        (_WA_ID, _TEMPLATE_WITH_HOTEL, "ar", ("فندق زمزم",))
    ]
    assert _outbound(db_conn) == [
        (
            "حاولنا نتواصل معك بخصوص طلبك في فندق زمزم. ردّ على هذه الرسالة "
            "متى ما ناسبك ونكمل معك إن شاء الله.",
            reply_id,
        )
    ]
    assert _reply_state(db_conn, reply_id) == (True, True, False, None)


@pytest.mark.usefixtures("template_names")
def test_a_template_without_a_hotel_uses_the_variant_with_no_hotel_name(
    wiring: _Wiring, db_conn: psycopg.Connection[Any]
) -> None:
    _, takeover_id = _taken_over_conversation(
        db_conn, customer_wrote_ago=_OUTSIDE_WINDOW
    )
    reply_id = seed_staff_template_reply(db_conn, takeover_id)

    response = _send(wiring.client, reply_id)

    assert response.json() == {"status": "sent"}
    assert wiring.sender.template_calls == [(_WA_ID, _TEMPLATE_NO_HOTEL, "en", ())]
    assert _outbound(db_conn) == [
        (
            "We tried to reach you about your request. Reply to this message "
            "whenever it suits you and we will continue from there.",
            reply_id,
        )
    ]


@pytest.mark.usefixtures("template_names")
def test_a_template_goes_out_in_indonesian_for_an_indonesian_customer(
    wiring: _Wiring, db_conn: psycopg.Connection[Any]
) -> None:
    conversation_id, takeover_id = _taken_over_conversation(
        db_conn, customer_wrote_ago=_OUTSIDE_WINDOW
    )
    _customer_wrote(db_conn, conversation_id, "Halo, ada kamar?", _OUTSIDE_WINDOW)
    hotel_id = seed_hotel(db_conn, hotel_name="Hotel Dua")
    reply_id = seed_staff_template_reply(db_conn, takeover_id, hotel_id=hotel_id)

    _send(wiring.client, reply_id)

    assert wiring.sender.template_calls == [
        (_WA_ID, _TEMPLATE_WITH_HOTEL, "id", ("Hotel Dua",))
    ]


@pytest.mark.usefixtures("template_names")
def test_a_template_is_refused_while_the_24_hour_window_is_open(
    wiring: _Wiring, db_conn: psycopg.Connection[Any]
) -> None:
    """Inside the window free text is the right tool; the template is
    recorded as failed (window_open) and nothing is sent."""
    _, takeover_id = _taken_over_conversation(db_conn)
    reply_id = seed_staff_template_reply(db_conn, takeover_id)

    response = _send(wiring.client, reply_id)

    assert (response.status_code, response.json()) == (200, {"status": "window_open"})
    assert wiring.sender.template_calls == []
    assert wiring.sender.calls == []
    assert _outbound(db_conn) == []
    assert _reply_state(db_conn, reply_id) == (True, False, True, "window_open")


@pytest.mark.usefixtures("template_names_unset")
def test_a_template_claims_nothing_while_the_feature_is_off(
    wiring: _Wiring, db_conn: psycopg.Connection[Any]
) -> None:
    _, takeover_id = _taken_over_conversation(
        db_conn, customer_wrote_ago=_OUTSIDE_WINDOW
    )
    reply_id = seed_staff_template_reply(db_conn, takeover_id)

    response = _send(wiring.client, reply_id)

    assert (response.status_code, response.json()) == (
        200,
        {"status": "not_configured"},
    )
    assert wiring.sender.template_calls == []
    assert wiring.sender.calls == []
    assert _reply_state(db_conn, reply_id) == (False, False, False, None)


def test_a_half_set_configuration_is_off_and_never_sends_the_placeholder(
    wiring: _Wiring,
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHATSAPP_REENGAGEMENT_TEMPLATE", _TEMPLATE_WITH_HOTEL)
    monkeypatch.delenv("WHATSAPP_REENGAGEMENT_TEMPLATE_NO_HOTEL", raising=False)
    _, takeover_id = _taken_over_conversation(
        db_conn, customer_wrote_ago=_OUTSIDE_WINDOW
    )
    reply_id = seed_staff_template_reply(db_conn, takeover_id)

    response = _send(wiring.client, reply_id)

    assert response.json() == {"status": "not_configured"}
    assert wiring.sender.template_calls == []
    assert wiring.sender.calls == []


@pytest.mark.usefixtures("template_names")
def test_a_failed_template_send_is_recorded_and_never_retried(
    wiring: _Wiring, db_conn: psycopg.Connection[Any]
) -> None:
    _, takeover_id = _taken_over_conversation(
        db_conn, customer_wrote_ago=_OUTSIDE_WINDOW
    )
    reply_id = seed_staff_template_reply(db_conn, takeover_id)
    wiring.sender.fail = True

    first = _send(wiring.client, reply_id)
    second = _send(wiring.client, reply_id)

    assert (first.status_code, first.json()) == (502, {"status": "failed"})
    assert second.json() == {"status": "already_claimed"}
    assert len(wiring.sender.template_calls) == 1
    assert _outbound(db_conn) == []
    assert _reply_state(db_conn, reply_id) == (True, False, True, "send_failed")


@pytest.mark.usefixtures("template_names")
def test_a_template_whose_takeover_ended_is_not_sent(
    wiring: _Wiring, db_conn: psycopg.Connection[Any]
) -> None:
    _, takeover_id = _taken_over_conversation(
        db_conn, customer_wrote_ago=_OUTSIDE_WINDOW
    )
    reply_id = seed_staff_template_reply(db_conn, takeover_id)
    db_conn.execute(
        "UPDATE conversation_takeovers SET ended_at = now(), ended_by = taken_over_by, "
        "outcome = 'handed_back' WHERE id = %s",
        (takeover_id,),
    )

    response = _send(wiring.client, reply_id)

    assert response.json() == {"status": "takeover_ended"}
    assert wiring.sender.template_calls == []


def test_the_status_endpoint_says_whether_the_template_is_switched_on(
    wiring: _Wiring, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = {"Authorization": f"Bearer {_INTERNAL_TOKEN}"}
    monkeypatch.delenv("WHATSAPP_REENGAGEMENT_TEMPLATE", raising=False)
    monkeypatch.delenv("WHATSAPP_REENGAGEMENT_TEMPLATE_NO_HOTEL", raising=False)
    off = wiring.client.get("/internal/reengagement-template", headers=headers)

    monkeypatch.setenv("WHATSAPP_REENGAGEMENT_TEMPLATE", _TEMPLATE_WITH_HOTEL)
    monkeypatch.setenv("WHATSAPP_REENGAGEMENT_TEMPLATE_NO_HOTEL", _TEMPLATE_NO_HOTEL)
    on = wiring.client.get("/internal/reengagement-template", headers=headers)

    monkeypatch.delenv("WHATSAPP_REENGAGEMENT_TEMPLATE_NO_HOTEL")
    half = wiring.client.get("/internal/reengagement-template", headers=headers)

    assert off.json() == {"enabled": False}
    assert on.json() == {"enabled": True}
    assert half.json() == {"enabled": False}


def test_the_status_endpoint_needs_the_internal_token(wiring: _Wiring) -> None:
    missing = wiring.client.get("/internal/reengagement-template")
    wrong = wiring.client.get(
        "/internal/reengagement-template", headers={"Authorization": "Bearer nope"}
    )

    assert (missing.status_code, wrong.status_code) == (401, 401)
