"""A taken-over conversation end to end, through the real app against a
real Postgres (staff notification step 2a, owner decisions D5 and D6,
2026-10-02): the bot stays silent -- at the webhook's fast path, and for a
turn the takeover overtakes -- and the internal endpoint sends the one
acknowledgement.

Every test runs as the privileged test role and as hotel_agent, the role
the agent connects as (the client fixture). The WhatsApp sender and the
model are fakes, as in tests/integration/test_webhook.py; the output guard
and the database are real.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from services.agent import takeover as takeover_module
from services.agent import takeover_ack as takeover_ack_module
from services.agent import webhook as webhook_module
from services.agent.fixed_texts import TAKEN_OVER
from services.agent.llm.config import LlmSettings
from services.agent.llm.errors import ModelUnavailableError
from services.agent.llm.model_types import ModelResponse, ModelTurn, ModelUsage, Turn
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
    seed_message,
    seed_takeover,
)

pytestmark = pytest.mark.usefixtures("db_conn")

_APP_SECRET = "test-app-secret"
_INTERNAL_TOKEN = "test-agent-internal-token-placeholder"
_PHONE = "+966500000001"
_WA_ID = "966500000001"
_TEST_WHATSAPP_SETTINGS = WhatsAppSendSettings(
    phone_number_id="test-phone-number-id",
    access_token="test-access-token",
    timeout_ms=10_000,
)
_MODEL_REPLY = "Sure, which dates?"


def _settings(*, max_messages_per_number_per_day: int = 1_000) -> LlmSettings:
    return LlmSettings(
        model="test-model-v1",
        api_key="test-key",
        timeout_ms=20_000,
        max_conversation_turns=20,
        max_tokens_per_conversation=1_000_000,
        max_spend_per_day_usd=Decimal("1000"),
        max_messages_per_number_per_day=max_messages_per_number_per_day,
        max_tokens_per_number_per_day=10_000_000,
    )


def _usage() -> ModelUsage:
    return ModelUsage(prompt_tokens=25, candidates_tokens=5, total_tokens=30)


@dataclass
class _Transport:
    """Replies with _MODEL_REPLY -- after running `during_call` (a takeover
    landing while the model thinks), and raising `error` if set."""

    during_call: Callable[[], None] | None = None
    error: Exception | None = None
    calls: list[str] = field(default_factory=list)

    async def generate(
        self, *, turns: list[Turn], system_instruction: str, deadline: float
    ) -> ModelResponse:
        del turns, system_instruction, deadline
        self.calls.append("call")
        if self.during_call is not None:
            self.during_call()
        if self.error is not None:
            raise self.error
        return ModelResponse(
            turn=ModelTurn(text=_MODEL_REPLY, tool_calls=()), usage=_usage()
        )


@dataclass
class _RecordingSender:
    """Records every send, with a distinct message id per success
    (messages.whatsapp_message_id is unique); fails every send if told to."""

    fail: bool = False
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def send_text(self, *, to_phone: str, body: str) -> str:
        self.calls.append((to_phone, body))
        if self.fail:
            raise WhatsAppSendError("simulated API error")
        return f"wamid.OUT-{len(self.calls)}"

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
    transport: _Transport


@pytest.fixture(params=["privileged", "hotel_agent"])
def wiring(
    request: pytest.FixtureRequest,
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> _Wiring:
    """The real app with fixed secrets, a recording sender and a fake model,
    on the test's own connection or on a fresh one as hotel_agent."""
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
        webhook_module,
        "get_webhook_settings",
        lambda: webhook_module.WebhookSettings(
            verify_token="test-verify-token", app_secret=_APP_SECRET
        ),
    )
    monkeypatch.setattr(
        takeover_ack_module,
        "get_internal_api_settings",
        lambda: InternalApiSettings(token=_INTERNAL_TOKEN),
    )
    monkeypatch.setattr(webhook_module, "get_llm_settings", _settings)
    monkeypatch.setattr(
        webhook_module, "get_whatsapp_send_settings", lambda: _TEST_WHATSAPP_SETTINGS
    )
    sender = _RecordingSender()
    monkeypatch.setattr(webhook_module, "get_whatsapp_sender", lambda _s: sender)
    transport = _Transport()
    monkeypatch.setattr(webhook_module, "get_model_transport", lambda _s: transport)
    return _Wiring(client=TestClient(app), sender=sender, transport=transport)


def _post_message(client: TestClient, message: dict[str, Any]) -> Any:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "test-waba-id",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [
                                {"wa_id": _WA_ID, "profile": {"name": "Customer"}}
                            ],
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(_APP_SECRET.encode("utf-8"), body, hashlib.sha256)
    return client.post(
        "/webhook/whatsapp",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={signature.hexdigest()}",
        },
    )


def _text(message_id: str, body: str = "a room for tonight?") -> dict[str, Any]:
    return {
        "from": _WA_ID,
        "id": message_id,
        "timestamp": "1700000000",
        "type": "text",
        "text": {"body": body},
    }


def _media(message_id: str, message_type: str) -> dict[str, Any]:
    return {
        "from": _WA_ID,
        "id": message_id,
        "timestamp": "1700000000",
        "type": message_type,
        message_type: {},
    }


def _taken_over_conversation(db_conn: psycopg.Connection[Any]) -> tuple[int, int]:
    """A customer with an open escalation and an active takeover; returns
    the conversation and takeover ids."""
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    seed_escalation(db_conn, conversation_id, reason="booking_requested")
    return conversation_id, seed_takeover(db_conn, conversation_id)


def _outbound_bodies(db_conn: psycopg.Connection[Any]) -> list[str]:
    rows = db_conn.execute(
        "SELECT body FROM messages WHERE direction = 'outbound' ORDER BY id"
    ).fetchall()
    return [body for (body,) in rows]


def _inbound_bodies(db_conn: psycopg.Connection[Any]) -> list[str]:
    rows = db_conn.execute(
        "SELECT body FROM messages WHERE direction = 'inbound' ORDER BY id"
    ).fetchall()
    return [body for (body,) in rows]


def _reasons(db_conn: psycopg.Connection[Any]) -> list[str]:
    rows = db_conn.execute("SELECT reason FROM escalations ORDER BY id").fetchall()
    return [reason for (reason,) in rows]


def _events(caplog: pytest.LogCaptureFixture, logger_name: str) -> list[dict[str, Any]]:
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == logger_name
    ]


def _turn_status(caplog: pytest.LogCaptureFixture) -> str:
    (finished,) = [
        event
        for event in _events(caplog, "services.agent.webhook")
        if event["event"] == "reply_turn_finished"
    ]
    status: str = finished["status"]
    return status


# ---------------------------------------------------------------------------
# The webhook while a conversation is taken over
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "stored"),
    [
        pytest.param(_text("wamid.in"), "a room for tonight?", id="text"),
        pytest.param(_media("wamid.in", "audio"), "[audio message]", id="voice-note"),
        pytest.param(_media("wamid.in", "image"), "[image message]", id="image"),
        pytest.param(_media("wamid.in", "video"), "[video message]", id="other-media"),
    ],
)
def test_a_message_to_a_taken_over_conversation_is_stored_and_nothing_is_sent(
    wiring: _Wiring,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
    message: dict[str, Any],
    stored: str,
) -> None:
    """Owner decision D6: no turn, no "please type", no fallback and no new
    escalation -- the holder sees the message on the dashboard."""
    conversation_id, _ = _taken_over_conversation(db_conn)
    caplog.set_level(logging.INFO, logger="services.agent.webhook")

    response = _post_message(wiring.client, message)

    assert response.status_code == 200
    assert response.json() == {"status": "taken_over"}
    assert wiring.transport.calls == []
    assert wiring.sender.calls == []
    assert _inbound_bodies(db_conn) == [stored]
    assert _outbound_bodies(db_conn) == []
    assert _reasons(db_conn) == ["booking_requested"]
    assert {
        "event": "inbound_while_taken_over",
        "conversation_id": conversation_id,
        "message_type": message["type"],
    } in _events(caplog, "services.agent.webhook")


def test_a_message_past_the_rate_cap_during_a_takeover_gets_no_fallback(
    wiring: _Wiring,
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id, _ = _taken_over_conversation(db_conn)
    seed_message(
        db_conn,
        conversation_id,
        direction="inbound",
        body="earlier",
        customer_phone=_PHONE,
    )
    monkeypatch.setattr(
        webhook_module,
        "get_llm_settings",
        lambda: _settings(max_messages_per_number_per_day=1),
    )

    response = _post_message(wiring.client, _text("wamid.over-the-cap"))

    assert response.json() == {"status": "taken_over"}
    assert wiring.sender.calls == []
    assert _reasons(db_conn) == ["booking_requested"]


def test_the_bot_answers_again_once_the_takeover_has_ended(
    wiring: _Wiring, db_conn: psycopg.Connection[Any]
) -> None:
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    seed_takeover(
        db_conn,
        conversation_id,
        taken_over_at=datetime.now(UTC) - timedelta(hours=1),
        ended_at=datetime.now(UTC) - timedelta(minutes=5),
    )

    response = _post_message(wiring.client, _text("wamid.after"))

    assert response.json() == {"status": "accepted"}
    assert wiring.transport.calls == ["call"]
    assert wiring.sender.calls == [(_WA_ID, _MODEL_REPLY)]


def test_a_failed_takeover_check_lets_the_bot_answer(
    wiring: _Wiring,
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Owner decision D6: a check that cannot be made is logged at ERROR and
    read as "not taken over" -- an answer is a smaller harm than silence."""
    _taken_over_conversation(db_conn)

    def _raise(_conn: psycopg.Connection[Any], **_kwargs: Any) -> bool:
        raise psycopg.OperationalError("simulated connection failure")

    monkeypatch.setattr(takeover_module, "active_takeover_exists", _raise)
    caplog.set_level(logging.ERROR, logger="services.agent.takeover")

    response = _post_message(wiring.client, _text("wamid.check-fails"))

    assert response.json() == {"status": "accepted"}
    assert wiring.sender.calls == [(_WA_ID, _MODEL_REPLY)]
    assert "takeover_check_failed" in [
        event["event"] for event in _events(caplog, "services.agent.takeover")
    ]


def test_a_takeover_during_the_turn_withholds_the_reply(
    wiring: _Wiring,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    seed_escalation(db_conn, conversation_id, reason="booking_requested")
    wiring.transport.during_call = lambda: _seed_takeover_now(db_conn, conversation_id)
    caplog.set_level(logging.INFO, logger="services.agent.webhook")

    response = _post_message(wiring.client, _text("wamid.overtaken"))

    assert response.json() == {"status": "accepted"}
    assert wiring.transport.calls == ["call"]
    assert wiring.sender.calls == []
    assert _outbound_bodies(db_conn) == []
    assert _reasons(db_conn) == ["booking_requested"]
    assert _turn_status(caplog) == "suppressed_taken_over"


def test_a_failed_turn_overtaken_by_a_takeover_escalates_but_sends_no_fallback(
    wiring: _Wiring,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The escalation still opens, for the holder to see; the customer gets
    nothing from the bot."""
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    wiring.transport.during_call = lambda: _seed_takeover_now(db_conn, conversation_id)
    wiring.transport.error = ModelUnavailableError("simulated outage")
    caplog.set_level(logging.INFO, logger="services.agent.webhook")

    _post_message(wiring.client, _text("wamid.failed-and-overtaken"))

    assert wiring.sender.calls == []
    assert _reasons(db_conn) == ["model_unavailable"]
    assert _turn_status(caplog) == "suppressed_taken_over"


def _seed_takeover_now(db_conn: psycopg.Connection[Any], conversation_id: int) -> None:
    seed_takeover(db_conn, conversation_id)


# ---------------------------------------------------------------------------
# The acknowledgement endpoint
# ---------------------------------------------------------------------------


def _acknowledge(
    client: TestClient, takeover_id: int, *, authorization: str | None = None
) -> Any:
    headers = {"Authorization": authorization or f"Bearer {_INTERNAL_TOKEN}"}
    return client.post(
        f"/internal/takeovers/{takeover_id}/acknowledge", headers=headers
    )


def _ack_columns(db_conn: psycopg.Connection[Any], takeover_id: int) -> tuple[Any, ...]:
    row = db_conn.execute(
        "SELECT ack_claimed_at IS NOT NULL, ack_sent_at IS NOT NULL, "
        "ack_failed_at IS NOT NULL FROM conversation_takeovers WHERE id = %s",
        (takeover_id,),
    ).fetchone()
    assert row is not None
    return tuple(row)


def _customer_wrote(
    db_conn: psycopg.Connection[Any],
    conversation_id: int,
    body: str,
    *,
    ago: timedelta = timedelta(minutes=5),
) -> None:
    seed_message(
        db_conn,
        conversation_id,
        direction="inbound",
        body=body,
        customer_phone=_PHONE,
        created_at=datetime.now(UTC) - ago,
    )


@pytest.mark.parametrize(
    ("customer_text", "notice"),
    [
        pytest.param("Is there a room tonight?", TAKEN_OVER.english, id="english"),
        pytest.param("فيه غرفة الليلة؟", TAKEN_OVER.arabic, id="arabic"),
    ],
)
def test_the_acknowledgement_is_sent_once_in_the_customers_language(
    wiring: _Wiring,
    db_conn: psycopg.Connection[Any],
    customer_text: str,
    notice: str,
) -> None:
    conversation_id, takeover_id = _taken_over_conversation(db_conn)
    _customer_wrote(db_conn, conversation_id, customer_text)

    first = _acknowledge(wiring.client, takeover_id)
    second = _acknowledge(wiring.client, takeover_id)

    assert (first.status_code, first.json()) == (200, {"status": "sent"})
    assert (second.status_code, second.json()) == (200, {"status": "already_claimed"})
    assert wiring.sender.calls == [(_WA_ID, notice)]
    assert _outbound_bodies(db_conn) == [notice]
    assert _ack_columns(db_conn, takeover_id) == (True, True, False)


def test_no_acknowledgement_for_a_takeover_that_already_ended(
    wiring: _Wiring, db_conn: psycopg.Connection[Any]
) -> None:
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    _customer_wrote(db_conn, conversation_id, "hello")
    takeover_id = seed_takeover(
        db_conn,
        conversation_id,
        taken_over_at=datetime.now(UTC) - timedelta(minutes=2),
        ended_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    response = _acknowledge(wiring.client, takeover_id)

    assert (response.status_code, response.json()) == (
        200,
        {"status": "takeover_ended"},
    )
    assert wiring.sender.calls == []


def test_an_unknown_takeover_is_not_found(wiring: _Wiring) -> None:
    response = _acknowledge(wiring.client, 999_999)

    assert (response.status_code, response.json()) == (404, {"status": "not_found"})
    assert wiring.sender.calls == []


def test_outside_the_24_hour_window_nothing_is_sent_and_it_is_recorded_as_failed(
    wiring: _Wiring, db_conn: psycopg.Connection[Any]
) -> None:
    """Meta refuses a free-form message there, possibly only later through
    a status webhook this system does not read -- so it is not attempted."""
    conversation_id, takeover_id = _taken_over_conversation(db_conn)
    _customer_wrote(db_conn, conversation_id, "hello", ago=timedelta(hours=25))

    response = _acknowledge(wiring.client, takeover_id)

    assert (response.status_code, response.json()) == (
        200,
        {"status": "outside_window"},
    )
    assert wiring.sender.calls == []
    assert _ack_columns(db_conn, takeover_id) == (True, False, True)


def test_a_failed_send_is_recorded_and_never_retried(
    wiring: _Wiring, db_conn: psycopg.Connection[Any]
) -> None:
    conversation_id, takeover_id = _taken_over_conversation(db_conn)
    _customer_wrote(db_conn, conversation_id, "hello")
    wiring.sender.fail = True

    first = _acknowledge(wiring.client, takeover_id)
    second = _acknowledge(wiring.client, takeover_id)

    assert (first.status_code, first.json()) == (502, {"status": "failed"})
    assert second.json() == {"status": "already_claimed"}
    assert len(wiring.sender.calls) == 1
    assert _ack_columns(db_conn, takeover_id) == (True, False, True)


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

    response = wiring.client.post("/internal/takeovers/1/acknowledge", headers=headers)

    assert response.status_code == 401
    assert wiring.sender.calls == []
    logged = json.dumps(_events(caplog, "services.agent.takeover_ack"))
    assert _INTERNAL_TOKEN not in logged


def test_an_unreachable_database_answers_unavailable(
    wiring: _Wiring, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _unreachable() -> Any:
        raise psycopg.OperationalError("simulated connection failure")

    monkeypatch.setattr(webhook_module, "get_db_connection", _unreachable)

    response = _acknowledge(wiring.client, 1)

    assert (response.status_code, response.json()) == (503, {"status": "unavailable"})
