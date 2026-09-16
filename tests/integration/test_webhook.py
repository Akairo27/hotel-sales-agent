"""Integration tests for services/agent/webhook.py against a real Postgres
instance — signature verification, idempotent inbound logging, the two
CLAUDE.md §9 caps (per-conversation token spend, per-number-per-day
message rate), the output guard, and the outbound WhatsApp send (a fake
sender — see _FakeWhatsAppSender — stands in for the real network call,
the same way _FakeTransport already stands in for the real model).

TestClient drives the real ASGI app end to end rather than calling route
functions directly, so the ordering guarantee under test (signature checked
before any database access) is exercised as an actual HTTP request, not
assumed from reading the source.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from google.genai import types

from services.agent import webhook as webhook_module
from services.agent.llm.caps import record_token_usage
from services.agent.llm.client import ModelTransport
from services.agent.llm.config import MAX_TOOL_ITERATIONS, LlmSettings
from services.agent.llm.conversation import UsageTotals
from services.agent.llm.errors import ModelUnavailableError
from services.agent.main import app
from services.agent.output_guard.enforcement import (
    OUTPUT_GUARD_FALLBACK_MESSAGE,
    REASON_MISMATCH,
    GuardVerdict,
)
from services.agent.whatsapp_send import (
    WhatsAppSender,
    WhatsAppSendError,
    WhatsAppSendSettings,
)
from tests.integration._seed import (
    flat_demand_curve,
    flat_min_profit,
    seed_allotment_night,
    seed_allotment_nights,
    seed_conversation,
    seed_hotel_and_room_type,
    seed_message,
    seed_price_rule,
    seed_season,
)

pytestmark = pytest.mark.usefixtures("db_conn")

_APP_SECRET = "test-app-secret"
_VERIFY_TOKEN = "test-verify-token"
_PHONE = "+966500000001"
_WA_ID = "966500000001"
_OTHER_WA_ID = "966500000002"

# A check_availability call that always resolves (to {"available": False},
# since no matching inventory row exists in this module's fresh test
# schema) without raising -- used wherever a test needs a real, harmless
# tool call purely to keep generate_reply's loop going for another
# iteration.
_HARMLESS_AVAILABILITY_ARGS = {
    "hotel_id": 1,
    "room_type_id": 1,
    "check_in": "2026-01-01",
    "check_out": "2026-01-02",
    "rooms": 1,
}


def _settings(
    *,
    max_tokens_per_conversation: int = 1_000_000,
    max_spend_per_day_usd: Decimal = Decimal("1000"),
    max_messages_per_number_per_day: int = 1_000,
    max_conversation_turns: int = 20,
) -> LlmSettings:
    return LlmSettings(
        model="test-model-v1",
        api_key="test-key",
        timeout_ms=20_000,
        max_conversation_turns=max_conversation_turns,
        max_tokens_per_conversation=max_tokens_per_conversation,
        max_spend_per_day_usd=max_spend_per_day_usd,
        max_messages_per_number_per_day=max_messages_per_number_per_day,
    )


@dataclass
class _FakeTransport:
    """Always returns a plain text reply with the given token counts — no
    function calls, so generate_reply returns after exactly one call."""

    prompt_tokens: int = 50
    candidates_tokens: int = 10
    calls: list[str] = field(default_factory=list)

    async def generate(
        self, *, contents: list[types.Content], system_instruction: str
    ) -> types.GenerateContentResponse:
        del contents, system_instruction
        self.calls.append("call")
        content = types.Content(
            role="model", parts=[types.Part.from_text(text="hello from the model")]
        )
        return types.GenerateContentResponse(
            candidates=[types.Candidate(content=content)],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=self.prompt_tokens,
                candidates_token_count=self.candidates_tokens,
                total_token_count=self.prompt_tokens + self.candidates_tokens,
            ),
        )


@dataclass
class _NoUsageTransport:
    """Returns a reply with no usage_metadata at all — the exact shape
    conversation.py's _usage_from raises UsageUnavailableError against
    (tests/unit/test_llm_conversation.py covers that function directly;
    this double exists only to drive that path through the real webhook
    endpoint)."""

    calls: list[str] = field(default_factory=list)

    async def generate(
        self, *, contents: list[types.Content], system_instruction: str
    ) -> types.GenerateContentResponse:
        del contents, system_instruction
        self.calls.append("call")
        content = types.Content(
            role="model", parts=[types.Part.from_text(text="hello from the model")]
        )
        return types.GenerateContentResponse(
            candidates=[types.Candidate(content=content)], usage_metadata=None
        )


@dataclass
class _ToolCallingTransport:
    """Always calls check_availability with a fixed, real-dispatched set
    of args -- no hotel/room-type/inventory rows exist in this module's
    fresh, truncated test schema (tests/conftest.py's db_conn fixture), so
    dispatch_tool runs for real and returns {"available": False} rather
    than raising, and generate_reply's tool-calling loop keeps iterating.
    This drives several real model calls in one turn, so the mid-loop
    spend-cap recheck (conversation.py) can be exercised end to end
    through the real webhook against a real, non-mocked
    check_token_spend_caps -- not just at the wiring level
    (tests/unit/test_llm_conversation.py) or the caps.py-arithmetic level
    (tests/integration/test_llm_caps.py)."""

    prompt_tokens: int
    candidates_tokens: int
    calls: list[str] = field(default_factory=list)

    async def generate(
        self, *, contents: list[types.Content], system_instruction: str
    ) -> types.GenerateContentResponse:
        del contents, system_instruction
        self.calls.append("call")
        content = types.Content(
            role="model",
            parts=[
                types.Part.from_function_call(
                    name="check_availability",
                    args={
                        "hotel_id": 1,
                        "room_type_id": 1,
                        "check_in": "2026-01-01",
                        "check_out": "2026-01-02",
                        "rooms": 1,
                    },
                )
            ],
        )
        return types.GenerateContentResponse(
            candidates=[types.Candidate(content=content)],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=self.prompt_tokens,
                candidates_token_count=self.candidates_tokens,
                total_token_count=self.prompt_tokens + self.candidates_tokens,
            ),
        )


def _function_call_response(
    name: str,
    args: dict[str, Any],
    *,
    prompt_tokens: int,
    candidates_tokens: int,
) -> types.GenerateContentResponse:
    """One real, billed model call whose response is a tool call —
    dispatch_tool runs it for real against this module's fresh test
    schema (no monkeypatching), so a call to a bad tool name or with bad
    arguments raises the real UnknownToolError/InvalidToolArgumentsError,
    and a valid check_availability call against nonexistent inventory
    keeps generate_reply's loop going without raising anything."""
    content = types.Content(
        role="model", parts=[types.Part.from_function_call(name=name, args=args)]
    )
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=content)],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt_tokens,
            candidates_token_count=candidates_tokens,
            total_token_count=prompt_tokens + candidates_tokens,
        ),
    )


@dataclass
class _ScriptedTransport:
    """Returns (or raises) each scripted item in order, one per call to
    generate() -- one reusable fake standing in for a bespoke dataclass
    per exception type under test. Each script item is either a real,
    billed response (a types.GenerateContentResponse, whose usage is
    always counted by conversation.py before anything else happens with
    it) or an exception the transport layer itself raises directly
    (ModelUnavailableError, or a stand-in for a completely unanticipated
    failure) -- exceptions dispatch_tool raises instead (UnknownToolError,
    InvalidToolArgumentsError, pricing misconfigurations) are triggered by
    scripting a function-call response naming a bad tool or bad
    arguments, not by raising from here."""

    script: list[types.GenerateContentResponse | BaseException]
    calls: list[str] = field(default_factory=list)

    async def generate(
        self, *, contents: list[types.Content], system_instruction: str
    ) -> types.GenerateContentResponse:
        del contents, system_instruction
        item = self.script[len(self.calls)]
        self.calls.append("call")
        if isinstance(item, BaseException):
            raise item
        return item


def _whatsapp_payload(
    *,
    wa_id: str,
    message_id: str,
    body: str,
    contact_name: str | None = "Test Customer",
) -> dict[str, Any]:
    contact: dict[str, Any] = {"wa_id": wa_id}
    if contact_name is not None:
        contact["profile"] = {"name": contact_name}
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "test-waba-id",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550000000",
                                "phone_number_id": "test-phone-number-id",
                            },
                            "contacts": [contact],
                            "messages": [
                                {
                                    "from": wa_id,
                                    "id": message_id,
                                    "timestamp": "1700000000",
                                    "text": {"body": body},
                                    "type": "text",
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


def _sign(body: bytes, app_secret: str = _APP_SECRET) -> str:
    digest = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _post(
    client: TestClient,
    payload: dict[str, Any],
    *,
    signature: str | None,
) -> Any:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return client.post("/webhook/whatsapp", content=body, headers=headers)


@contextlib.contextmanager
def _nullcontext(
    conn: psycopg.Connection[Any],
) -> Iterator[psycopg.Connection[Any]]:
    """A context manager over an already-open connection this fixture does
    not own — closing it is db_conn's own fixture teardown's job, not this
    module's, unlike the real get_db_connection which opens and closes a
    fresh connection per request."""
    yield conn


_TEST_WHATSAPP_SETTINGS = WhatsAppSendSettings(
    phone_number_id="test-phone-number-id",
    access_token="test-access-token",
    timeout_ms=10_000,
)


@dataclass
class _FakeWhatsAppSender:
    """Always succeeds with a fixed message id and no network access —
    the default webhook_client wires in, so the many tests that don't
    care about the send step itself (most of them) need no per-test
    setup for it, the same reasoning _FakeTransport's default existence
    covers for the model. Tests that do care use _set_whatsapp_sender to
    swap in their own instance (or a failing one) and inspect .calls
    afterward."""

    message_id: str = "wamid.OUTBOUND-DEFAULT"
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def send_text(self, *, to_phone: str, body: str) -> str:
        self.calls.append((to_phone, body))
        return self.message_id


@dataclass
class _FailingWhatsAppSender:
    """Always raises -- for tests exercising webhook.py's send-failure
    handling. WhatsAppSendError is deliberately not the only exception
    type this needs to prove is caught (see webhook.py's own
    _send_or_log_failure docstring for why its catch is broad), so
    individual tests construct this with whatever exception they want to
    prove gets caught."""

    exc: BaseException

    async def send_text(self, *, to_phone: str, body: str) -> str:
        del to_phone, body
        raise self.exc


@pytest.fixture
def webhook_client(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Wires the real app to the test's own Postgres connection and a
    fixed webhook secret, via plain monkeypatch — this module never uses
    FastAPI's dependency_overrides, so this is the same mechanism the
    module itself is tested with everywhere else in this repo.

    Also wires a default, always-succeeding, no-network WhatsApp sender
    (_FakeWhatsAppSender) — unlike get_llm_settings/get_model_transport
    below, which stay opt-in per test (different tests need different
    cap values), every test in this file either doesn't reach the send
    step at all or wants it to just work, so defaulting it here avoids
    repeating the same setup in every one of them.
    """
    monkeypatch.setattr(
        webhook_module,
        "get_webhook_settings",
        lambda: webhook_module.WebhookSettings(
            verify_token=_VERIFY_TOKEN, app_secret=_APP_SECRET
        ),
    )
    monkeypatch.setattr(
        webhook_module, "get_db_connection", lambda: _nullcontext(db_conn)
    )
    monkeypatch.setattr(
        webhook_module, "get_whatsapp_send_settings", lambda: _TEST_WHATSAPP_SETTINGS
    )
    monkeypatch.setattr(
        webhook_module, "get_whatsapp_sender", lambda _settings: _FakeWhatsAppSender()
    )
    yield TestClient(app)


def _set_llm_settings(monkeypatch: pytest.MonkeyPatch, settings: LlmSettings) -> None:
    monkeypatch.setattr(webhook_module, "get_llm_settings", lambda: settings)


def _set_transport(monkeypatch: pytest.MonkeyPatch, transport: ModelTransport) -> None:
    monkeypatch.setattr(
        webhook_module, "get_model_transport", lambda _settings: transport
    )


def _set_whatsapp_sender(
    monkeypatch: pytest.MonkeyPatch, sender: WhatsAppSender
) -> None:
    monkeypatch.setattr(webhook_module, "get_whatsapp_sender", lambda _settings: sender)


def _message_count(db_conn: psycopg.Connection[Any]) -> int:
    row = db_conn.execute("SELECT count(*) FROM messages").fetchone()
    assert row is not None
    return int(row[0])


def _conversation_count(db_conn: psycopg.Connection[Any]) -> int:
    row = db_conn.execute("SELECT count(*) FROM conversations").fetchone()
    assert row is not None
    return int(row[0])


def test_verify_subscription_returns_the_challenge_for_a_matching_token(
    webhook_client: TestClient,
) -> None:
    response = webhook_client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": _VERIFY_TOKEN,
            "hub.challenge": "echo-me-back",
        },
    )

    assert response.status_code == 200
    assert response.text == "echo-me-back"


def test_verify_subscription_rejects_a_wrong_token(
    webhook_client: TestClient,
) -> None:
    response = webhook_client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong-token",
            "hub.challenge": "echo-me-back",
        },
    )

    assert response.status_code == 403


def test_receive_message_with_valid_signature_processes_and_records_usage(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport(prompt_tokens=50, candidates_tokens=10)
    _set_transport(monkeypatch, transport)
    payload = _whatsapp_payload(wa_id=_WA_ID, message_id="wamid.1", body="hello")

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "processed"}
    assert len(transport.calls) == 1
    rows = db_conn.execute(
        "SELECT direction, body, whatsapp_message_id FROM messages "
        "WHERE customer_phone = %s ORDER BY direction",
        (_PHONE,),
    ).fetchall()
    assert rows == [
        ("inbound", "hello", "wamid.1"),
        ("outbound", "hello from the model", "wamid.OUTBOUND-DEFAULT"),
    ]
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (50, 10, 60)


def _text_response(
    text: str, *, prompt_tokens: int = 50, candidates_tokens: int = 10
) -> types.GenerateContentResponse:
    content = types.Content(role="model", parts=[types.Part.from_text(text=text)])
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=content)],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt_tokens,
            candidates_token_count=candidates_tokens,
            total_token_count=prompt_tokens + candidates_tokens,
        ),
    )


def test_receive_message_blocks_a_guard_violating_reply_and_sends_the_fallback(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    """A candidate reply stating a price with no matching quote (nothing
    was seeded for this conversation, so any stated amount is
    not_in_quotes) must never reach the customer -- the fallback is sent
    instead, through the real output guard end to end, not a stand-in for
    it."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _ScriptedTransport([_text_response("I can do 900.00 SAR for you")])
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    payload = _whatsapp_payload(wa_id=_WA_ID, message_id="wamid.guard-block", body="hi")

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "escalated"}
    assert len(transport.calls) == 1
    # The fallback was sent, not the guard-violating text.
    assert len(sender.calls) == 1
    assert sender.calls[0][1] == OUTPUT_GUARD_FALLBACK_MESSAGE
    outbound_row = db_conn.execute(
        "SELECT direction, body FROM messages "
        "WHERE customer_phone = %s AND direction = 'outbound'",
        (_PHONE,),
    ).fetchone()
    assert outbound_row == ("outbound", OUTPUT_GUARD_FALLBACK_MESSAGE)
    escalation_row = db_conn.execute(
        "SELECT reason FROM escalations WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert escalation_row == (REASON_MISMATCH,)
    # The model call already happened -- its usage must still be
    # recorded even though the reply itself was never sent.
    usage_row = db_conn.execute(
        "SELECT total_tokens FROM token_usage WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert usage_row == (60,)


def test_receive_message_returns_send_failed_when_the_whatsapp_send_fails(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The guard allows this reply (no stated price at all); the send
    itself is what fails here. Usage from the already-successful model
    call must still be recorded, and no outbound message row should
    exist for a delivery that never actually happened."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport(prompt_tokens=50, candidates_tokens=10)
    _set_transport(monkeypatch, transport)
    _set_whatsapp_sender(
        monkeypatch, _FailingWhatsAppSender(WhatsAppSendError("simulated API error"))
    )
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(wa_id=_WA_ID, message_id="wamid.send-fails", body="hi")

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "send_failed"}
    outbound_row = db_conn.execute(
        "SELECT count(*) FROM messages "
        "WHERE customer_phone = %s AND direction = 'outbound'",
        (_PHONE,),
    ).fetchone()
    assert outbound_row == (0,)
    usage_row = db_conn.execute(
        "SELECT total_tokens FROM token_usage WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert usage_row == (60,)
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    logged = json.loads(error_records[0].getMessage())
    assert logged["event"] == "whatsapp_send_failed"
    assert logged["exception_type"] == "WhatsAppSendError"
    assert error_records[0].exc_info is not None


def test_receive_message_logs_and_returns_fallback_blocked_if_it_ever_happens(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Structurally impossible for the real OUTPUT_GUARD_FALLBACK_MESSAGE
    -- test_output_guard_fallback_message_is_always_allowed
    (tests/integration/test_output_guard.py) proves that end to end
    against the real guard -- so this test forces the scenario directly
    by faking enforce_outbound_text's verdict, to prove webhook.py's own
    handling of the branch rather than re-proving the guard's own
    invariant."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport(prompt_tokens=50, candidates_tokens=10)
    _set_transport(monkeypatch, transport)

    call_count = {"n": 0}

    def _fake_enforce(
        _conn: psycopg.Connection[Any], *, conversation_id: int, text: str
    ) -> GuardVerdict:
        del conversation_id, text
        call_count["n"] += 1
        escalation_id = 111 if call_count["n"] == 1 else 222
        return GuardVerdict(
            allowed=False, findings=(), quote_ids=(), escalation_id=escalation_id
        )

    monkeypatch.setattr(webhook_module, "enforce_outbound_text", _fake_enforce)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.fallback-blocked", body="hi"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "fallback_blocked"}
    assert call_count["n"] == 2
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    logged = json.loads(error_records[0].getMessage())
    assert logged["event"] == "fallback_message_blocked"
    assert logged["original_escalation_id"] == 111
    assert logged["fallback_escalation_id"] == 222


def test_receive_message_returns_delivery_failed_when_the_guard_check_itself_errors(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """enforce_outbound_text, get_whatsapp_send_settings, and
    get_whatsapp_sender all sit inside the try/except that produces
    "delivery_failed" -- this test forces the first of those to raise,
    proving the whole section is covered, not just the send call itself
    (already covered by test_receive_message_returns_send_failed_when_
    the_whatsapp_send_fails). Usage from the already-successful model
    call must still be recorded regardless."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport(prompt_tokens=50, candidates_tokens=10)
    _set_transport(monkeypatch, transport)

    def _fake_enforce(
        _conn: psycopg.Connection[Any], *, conversation_id: int, text: str
    ) -> None:
        del conversation_id, text
        raise RuntimeError("simulated guard-check database error")

    monkeypatch.setattr(webhook_module, "enforce_outbound_text", _fake_enforce)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.delivery-fails", body="hi"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "delivery_failed"}
    usage_row = db_conn.execute(
        "SELECT total_tokens FROM token_usage WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert usage_row == (60,)
    outbound_row = db_conn.execute(
        "SELECT count(*) FROM messages "
        "WHERE customer_phone = %s AND direction = 'outbound'",
        (_PHONE,),
    ).fetchone()
    assert outbound_row == (0,)
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    logged = json.loads(error_records[0].getMessage())
    assert logged["event"] == "reply_delivery_failed"
    assert logged["exception_type"] == "RuntimeError"
    assert error_records[0].exc_info is not None


def test_receive_message_returns_200_and_logs_when_usage_is_unavailable(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The model call already happened (real spend) by the time
    UsageUnavailableError is raised — a 500 here would make Meta retry,
    and the retry can only resolve as a duplicate (see the module
    docstring), permanently losing this call's usage. Must be 200, logged,
    not retried."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _NoUsageTransport()
    _set_transport(monkeypatch, transport)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(wa_id=_WA_ID, message_id="wamid.no-usage", body="hello")

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "usage_unavailable"}
    assert len(transport.calls) == 1
    conversation_row = db_conn.execute(
        "SELECT id FROM conversations WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert conversation_row is not None
    usage_row = db_conn.execute(
        "SELECT count(*) FROM token_usage WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert usage_row == (0,)

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    logged = json.loads(error_records[0].getMessage())
    assert logged["event"] == "usage_unavailable"
    assert logged["conversation_id"] == conversation_row[0]


def test_receive_message_returns_200_and_logs_when_record_token_usage_fails(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """record_token_usage failing after a successful model call is the
    same shape of risk as UsageUnavailableError: spend already happened,
    the row just never lands. A 500 would make Meta retry into a
    duplicate no-op that can never write the missing row (see the module
    docstring) — must be 200, logged, not retried."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport(prompt_tokens=50, candidates_tokens=10)
    _set_transport(monkeypatch, transport)

    def _raise_db_error(
        _conn: psycopg.Connection[Any],
        *,
        conversation_id: int,
        customer_phone: str,
        usage: UsageTotals,
        now: datetime,
    ) -> None:
        del conversation_id, customer_phone, usage, now
        raise psycopg.OperationalError("simulated connection failure")

    monkeypatch.setattr(webhook_module, "record_token_usage", _raise_db_error)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.usage-write-fails", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "usage_not_recorded"}
    assert len(transport.calls) == 1
    conversation_row = db_conn.execute(
        "SELECT id FROM conversations WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert conversation_row is not None
    usage_row = db_conn.execute(
        "SELECT count(*) FROM token_usage WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert usage_row == (0,)

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    logged = json.loads(error_records[0].getMessage())
    assert logged["event"] == "record_token_usage_failed"
    assert logged["conversation_id"] == conversation_row[0]
    assert logged["prompt_tokens"] == 50
    assert logged["candidates_tokens"] == 10
    assert logged["total_tokens"] == 60
    assert logged["exception_type"] == "OperationalError"
    assert "simulated connection failure" in logged["exception_message"]
    assert error_records[0].exc_info is not None
    assert error_records[0].exc_text is not None
    assert "OperationalError" in error_records[0].exc_text


def test_receive_message_returns_200_when_record_token_usage_raises_a_non_db_error(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The except clause around record_token_usage is deliberately
    `except Exception`, not `except psycopg.Error` — a bug in this module
    or an unexpected value has the exact same permanent-gap consequence as
    a database error (see the module docstring), so it must be caught the
    same way. A RuntimeError proves the catch isn't narrowed to the
    database-error case the previous test already covers."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport(prompt_tokens=50, candidates_tokens=10)
    _set_transport(monkeypatch, transport)

    def _raise_unexpected_error(
        _conn: psycopg.Connection[Any],
        *,
        conversation_id: int,
        customer_phone: str,
        usage: UsageTotals,
        now: datetime,
    ) -> None:
        del conversation_id, customer_phone, usage, now
        raise RuntimeError("simulated bug, not a database failure")

    monkeypatch.setattr(webhook_module, "record_token_usage", _raise_unexpected_error)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.usage-write-bug", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "usage_not_recorded"}
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    logged = json.loads(error_records[0].getMessage())
    assert logged["event"] == "record_token_usage_failed"
    assert logged["exception_type"] == "RuntimeError"
    assert logged["exception_message"] == "simulated bug, not a database failure"
    assert error_records[0].exc_info is not None
    assert error_records[0].exc_text is not None
    assert "simulated bug, not a database failure" in error_records[0].exc_text


def test_receive_message_still_processes_when_increment_turn_count_fails(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mirrors test_receive_message_returns_200_and_logs_when_record_
    token_usage_fails for the sibling write: increment_turn_count failing
    must not turn a successfully delivered reply into a 500, and must not
    stop the reply from being recorded and sent -- only turn_count itself
    is missing, logged loudly rather than silently."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport(prompt_tokens=50, candidates_tokens=10)
    _set_transport(monkeypatch, transport)

    def _raise_db_error(
        _conn: psycopg.Connection[Any], *, conversation_id: int
    ) -> None:
        del conversation_id
        raise psycopg.OperationalError("simulated connection failure")

    monkeypatch.setattr(webhook_module, "increment_turn_count", _raise_db_error)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.turn-count-write-fails", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "processed"}
    conversation_row = db_conn.execute(
        "SELECT id, turn_count FROM conversations WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert conversation_row is not None
    assert conversation_row[1] == 0
    usage_row = db_conn.execute(
        "SELECT count(*) FROM token_usage WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert usage_row == (1,)

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    logged = json.loads(error_records[0].getMessage())
    assert logged["event"] == "increment_turn_count_failed"
    assert logged["conversation_id"] == conversation_row[0]
    assert logged["exception_type"] == "OperationalError"
    assert "simulated connection failure" in logged["exception_message"]
    assert error_records[0].exc_info is not None


def test_receive_message_with_invalid_signature_is_rejected_with_no_trace(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport()
    _set_transport(monkeypatch, transport)
    payload = _whatsapp_payload(wa_id=_WA_ID, message_id="wamid.bad-sig", body="hello")

    response = _post(webhook_client, payload, signature=_sign(b"not-the-real-body"))

    assert response.status_code == 401
    assert _message_count(db_conn) == 0
    assert _conversation_count(db_conn) == 0
    assert transport.calls == []


def test_receive_message_with_missing_signature_is_rejected_with_no_trace(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport()
    _set_transport(monkeypatch, transport)
    payload = _whatsapp_payload(wa_id=_WA_ID, message_id="wamid.no-sig", body="hello")

    response = _post(webhook_client, payload, signature=None)

    assert response.status_code == 401
    assert _message_count(db_conn) == 0
    assert _conversation_count(db_conn) == 0
    assert transport.calls == []


def test_receive_message_escalates_and_sends_fallback_when_the_spend_cap_is_exceeded(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    """Same "beyond the cap, escalate to a human" property as the turn
    cap (test_receive_message_escalates_and_sends_fallback_when_the_
    turn_cap_is_exceeded above), for TokenSpendCapExceededError: this cap
    has been live since before turn_count existed, so the silent-cap gap
    it shared with the turn cap was not hypothetical."""
    settings = _settings(max_tokens_per_conversation=50)
    _set_llm_settings(monkeypatch, settings)
    transport = _FakeTransport()
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    # Pre-existing usage already at the cap, recorded directly (not via the
    # webhook) — the point of this test is what happens on the *next*
    # inbound message against an already-capped conversation.
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    record_token_usage(
        db_conn,
        conversation_id=conversation_id,
        customer_phone=_PHONE,
        usage=UsageTotals(50, 0, 50),
        now=datetime(2026, 9, 1, tzinfo=UTC),
    )
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.capped", body="hi again"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "escalated"}
    assert transport.calls == []
    assert len(sender.calls) == 1
    assert sender.calls[0][1] == OUTPUT_GUARD_FALLBACK_MESSAGE
    inbound_row = db_conn.execute(
        "SELECT whatsapp_message_id FROM messages "
        "WHERE conversation_id = %s AND direction = 'inbound'",
        (conversation_id,),
    ).fetchone()
    assert inbound_row == ("wamid.capped",)
    outbound_row = db_conn.execute(
        "SELECT direction, body FROM messages "
        "WHERE conversation_id = %s AND direction = 'outbound'",
        (conversation_id,),
    ).fetchone()
    assert outbound_row == ("outbound", OUTPUT_GUARD_FALLBACK_MESSAGE)
    escalation_row = db_conn.execute(
        "SELECT reason FROM escalations WHERE conversation_id = %s",
        (conversation_id,),
    ).fetchone()
    assert escalation_row == ("token_spend_cap_exceeded",)
    # The cap was already at its limit before this turn made any model
    # call at all (usage_so_far is zero) — nothing new to record, so the
    # only token_usage row is the one seeded directly above, not a second
    # one from this request.
    usage_row_count = db_conn.execute(
        "SELECT count(*) FROM token_usage WHERE conversation_id = %s",
        (conversation_id,),
    ).fetchone()
    assert usage_row_count == (1,)


def test_receive_message_escalates_and_sends_fallback_when_the_turn_cap_is_exceeded(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    """CLAUDE.md §9's "beyond the cap, escalate to a human" -- a
    conversation already at its turn cap must not just get a logged
    "capped" status: a human escalation opens, and the customer gets the
    same bilingual fallback message a blocked reply gets, not silence.
    Mirrors test_receive_message_blocks_a_guard_violating_reply_and_
    sends_the_fallback's shape exactly, since this is the same
    "never leave the customer with nothing" property applied to a
    different trigger."""
    settings = _settings(max_conversation_turns=3)
    _set_llm_settings(monkeypatch, settings)
    transport = _FakeTransport()
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    # Already at the cap before this turn -- the point of this test is
    # what happens on the *next* inbound message against it.
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE, turn_count=3)
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.turn-capped", body="one more thing"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "escalated"}
    assert transport.calls == []
    assert len(sender.calls) == 1
    assert sender.calls[0][1] == OUTPUT_GUARD_FALLBACK_MESSAGE
    outbound_row = db_conn.execute(
        "SELECT direction, body FROM messages "
        "WHERE conversation_id = %s AND direction = 'outbound'",
        (conversation_id,),
    ).fetchone()
    assert outbound_row == ("outbound", OUTPUT_GUARD_FALLBACK_MESSAGE)
    escalation_row = db_conn.execute(
        "SELECT reason FROM escalations WHERE conversation_id = %s",
        (conversation_id,),
    ).fetchone()
    assert escalation_row == ("turn_cap_exceeded",)
    # No model call happened this turn -- the cap was already at its
    # limit before generate_reply ever loaded the message window, so
    # turn_count must not have been bumped past what was seeded.
    turn_count_row = db_conn.execute(
        "SELECT turn_count FROM conversations WHERE id = %s", (conversation_id,)
    ).fetchone()
    assert turn_count_row == (3,)


def test_receive_message_returns_capped_if_the_turn_cap_fallback_is_ever_blocked(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Structurally impossible for the real OUTPUT_GUARD_FALLBACK_MESSAGE
    -- test_output_guard_fallback_message_is_always_allowed
    (tests/integration/test_output_guard.py) proves that end to end --
    so this test forces the scenario directly by faking
    enforce_outbound_text's verdict, mirroring test_receive_message_
    logs_and_returns_fallback_blocked_if_it_ever_happens for the guard
    path, to prove _escalate_and_notify's own handling of the
    branch."""
    settings = _settings(max_conversation_turns=1)
    _set_llm_settings(monkeypatch, settings)
    seed_conversation(db_conn, customer_phone=_PHONE, turn_count=1)

    def _fake_enforce(
        _conn: psycopg.Connection[Any], *, conversation_id: int, text: str
    ) -> GuardVerdict:
        del conversation_id, text
        return GuardVerdict(allowed=False, findings=(), quote_ids=(), escalation_id=999)

    monkeypatch.setattr(webhook_module, "enforce_outbound_text", _fake_enforce)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.turn-cap-fallback-blocked", body="hi"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "capped"}
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    logged = [json.loads(r.getMessage()) for r in error_records]
    events = [entry["event"] for entry in logged]
    assert "conversation_escalated" in events
    assert "conversation_escalation_fallback_blocked" in events
    assert all(entry["reason"] == "turn_cap_exceeded" for entry in logged)


def test_receive_message_returns_capped_when_turn_cap_escalation_itself_errors(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """open_escalation, enforce_outbound_text, and the WhatsApp send all
    sit inside _escalate_and_notify's own try/except -- this test
    forces the first of those to raise, mirroring test_receive_message_
    returns_delivery_failed_when_the_guard_check_itself_errors for the
    normal reply path, to prove this new branch never lets a 500 escape
    either."""
    settings = _settings(max_conversation_turns=1)
    _set_llm_settings(monkeypatch, settings)
    seed_conversation(db_conn, customer_phone=_PHONE, turn_count=1)

    def _fake_open_escalation(
        _conn: psycopg.Connection[Any], *, conversation_id: int, reason: str, notes: Any
    ) -> int:
        del conversation_id, reason, notes
        raise RuntimeError("simulated escalation-insert database error")

    monkeypatch.setattr(webhook_module, "open_escalation", _fake_open_escalation)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.turn-cap-escalation-fails", body="hi"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "capped"}
    escalation_count = db_conn.execute(
        "SELECT count(*) FROM escalations WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert escalation_count == (0,)
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    logged = json.loads(error_records[0].getMessage())
    assert logged["event"] == "conversation_escalation_failed"
    assert logged["reason"] == "turn_cap_exceeded"
    assert logged["exception_type"] == "RuntimeError"
    assert error_records[0].exc_info is not None


def test_receive_message_increments_turn_count_after_a_normal_turn(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    """The turn cap has no effect unless a real, successful turn actually
    advances turn_count -- this is the direct proof for the success
    path; test_receive_message_records_full_usage_when_the_tool_loop_
    limit_is_exceeded covers the same write for a turn that made real
    model calls but never produced a sendable reply."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport(prompt_tokens=50, candidates_tokens=10)
    _set_transport(monkeypatch, transport)
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.turn-increments", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "processed"}
    turn_count_row = db_conn.execute(
        "SELECT turn_count FROM conversations WHERE id = %s", (conversation_id,)
    ).fetchone()
    assert turn_count_row == (1,)


def test_receive_message_records_partial_usage_when_the_cap_crosses_mid_turn(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    """conversation.py now re-checks the spend cap before every model call
    in its tool-calling loop, not just once before it — so a turn that
    starts under the cap can still cross it mid-loop, after one or more
    real (already-paid-for) model calls happened earlier in the same
    turn. That usage must not be discarded: TokenSpendCapExceededError
    carries it as usage_so_far, and the webhook records it before
    escalating and sending the fallback — proven here end to end,
    through the real endpoint and a real, non-mocked
    check_token_spend_caps, not just at the wiring level
    (tests/unit/test_llm_conversation.py) or the caps.py-arithmetic
    level (tests/integration/test_llm_caps.py)."""
    # Each real model call reports 30 tokens. The 50-token cap is still
    # under after 1 call (30) but crossed by the pre-check before a 3rd
    # call would happen (60 >= 50) — so exactly 2 model calls should
    # happen, and the recorded row should cover exactly those two.
    settings = _settings(max_tokens_per_conversation=50)
    _set_llm_settings(monkeypatch, settings)
    transport = _ToolCallingTransport(prompt_tokens=25, candidates_tokens=5)
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.crosses-mid-turn", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "escalated"}
    assert len(transport.calls) == 2
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (50, 10, 60)
    assert len(sender.calls) == 1
    assert sender.calls[0][1] == OUTPUT_GUARD_FALLBACK_MESSAGE
    escalation_row = db_conn.execute(
        "SELECT reason FROM escalations WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert escalation_row == ("token_spend_cap_exceeded",)


def test_receive_message_records_partial_usage_when_the_daily_cap_crosses_mid_turn(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    """Same shape of gap as the per-conversation cap above, for the
    global daily cap: DailySpendCapExceededError can also now fire after
    real model calls already happened this turn, and must carry (and the
    webhook must record) that usage before escalating and sending the
    fallback."""
    # 1000 prompt tokens costs 1000 * $0.75 / 1_000_000 = $0.00075 -- two
    # calls of 500 prompt tokens each cross that cap exactly on the
    # pre-check before a 3rd call would happen.
    settings = _settings(max_spend_per_day_usd=Decimal("0.00075"))
    _set_llm_settings(monkeypatch, settings)
    transport = _ToolCallingTransport(prompt_tokens=500, candidates_tokens=0)
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.daily-crosses-mid-turn", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "escalated"}
    assert len(transport.calls) == 2
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (1000, 0, 1000)
    assert len(sender.calls) == 1
    assert sender.calls[0][1] == OUTPUT_GUARD_FALLBACK_MESSAGE
    escalation_row = db_conn.execute(
        "SELECT reason FROM escalations WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert escalation_row == ("daily_spend_cap_exceeded",)


def test_receive_message_records_partial_usage_when_usage_is_unavailable_mid_turn(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """UsageUnavailableError raised on the *second* real model call in a
    turn is the gap the structural fix closes: the first call's usage was
    already known, sitting in generate_reply's own accumulator, before
    the second call's response came back unusable. Unlike the
    single-call case (test_receive_message_returns_200_and_logs_when_
    usage_is_unavailable above), that first call's usage must now be
    recorded, not silently discarded."""
    _set_llm_settings(monkeypatch, _settings())
    unusable_response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model", parts=[types.Part.from_text(text="irrelevant")]
                )
            )
        ],
        usage_metadata=None,
    )
    transport = _ScriptedTransport(
        [
            _function_call_response(
                "check_availability",
                _HARMLESS_AVAILABILITY_ARGS,
                prompt_tokens=25,
                candidates_tokens=5,
            ),
            unusable_response,
        ]
    )
    _set_transport(monkeypatch, transport)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.usage-unavailable-mid-turn", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "usage_unavailable"}
    assert len(transport.calls) == 2
    conversation_row = db_conn.execute(
        "SELECT id FROM conversations WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert conversation_row is not None
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (25, 5, 30)
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    logged = json.loads(error_records[0].getMessage())
    assert logged["event"] == "usage_unavailable"
    assert logged["conversation_id"] == conversation_row[0]


def test_receive_message_escalates_and_sends_fallback_when_the_transport_fails_mid_turn(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ModelUnavailableError only ever reaches generate_reply's caller
    after client.py's own retries are exhausted (this fake represents
    that final, already-retried failure, not a single raw attempt) --
    same "beyond the cap, escalate to a human" treatment the three
    CLAUDE.md §9 caps get: a customer left silent by a transient model
    failure is the same bad outcome as one left silent by a cap, so this
    escalates and sends the fallback rather than just logging
    "turn_failed". The first call's real usage must still be recorded."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _ScriptedTransport(
        [
            _function_call_response(
                "check_availability",
                _HARMLESS_AVAILABILITY_ARGS,
                prompt_tokens=25,
                candidates_tokens=5,
            ),
            ModelUnavailableError("simulated transport failure"),
        ]
    )
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.transport-fails-mid-turn", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "escalated"}
    assert len(transport.calls) == 2
    conversation_row = db_conn.execute(
        "SELECT id FROM conversations WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert conversation_row is not None
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (25, 5, 30)
    assert len(sender.calls) == 1
    assert sender.calls[0][1] == OUTPUT_GUARD_FALLBACK_MESSAGE
    escalation_row = db_conn.execute(
        "SELECT reason FROM escalations WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert escalation_row == ("model_unavailable",)
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    events = [json.loads(r.getMessage())["event"] for r in error_records]
    assert "conversation_escalated" in events


def test_receive_message_records_partial_usage_for_an_unknown_tool_call(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """UnknownToolError -- dispatch.py's own docstring calls this "an
    expected failure mode of a function-calling model, not a bug" -- can
    fire on the *first* iteration, since dispatch_tool runs after that
    call's own usage was already counted (conversation.py adds usage
    before checking function_calls). One iteration is enough to prove
    the gap; no second call is needed."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _ScriptedTransport(
        [
            _function_call_response(
                "not_a_real_tool", {}, prompt_tokens=25, candidates_tokens=5
            )
        ]
    )
    _set_transport(monkeypatch, transport)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.unknown-tool", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "turn_failed"}
    assert len(transport.calls) == 1
    conversation_row = db_conn.execute(
        "SELECT id FROM conversations WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert conversation_row is not None
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (25, 5, 30)
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    logged = json.loads(error_records[0].getMessage())
    assert logged["event"] == "turn_failed"
    assert logged["exception_type"] == "UnknownToolError"
    assert error_records[0].exc_info is not None
    assert error_records[0].exc_text is not None
    assert "UnknownToolError" in error_records[0].exc_text


def test_receive_message_records_partial_usage_for_invalid_tool_arguments(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """InvalidToolArgumentsError -- same "expected model failure mode,
    not a bug" category as UnknownToolError above, triggered here by a
    non-integer hotel_id a real function-calling model can plausibly
    hallucinate."""
    _set_llm_settings(monkeypatch, _settings())
    bad_args = dict(_HARMLESS_AVAILABILITY_ARGS, hotel_id="not-an-int")
    transport = _ScriptedTransport(
        [
            _function_call_response(
                "check_availability", bad_args, prompt_tokens=25, candidates_tokens=5
            )
        ]
    )
    _set_transport(monkeypatch, transport)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.invalid-tool-args", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "turn_failed"}
    assert len(transport.calls) == 1
    conversation_row = db_conn.execute(
        "SELECT id FROM conversations WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert conversation_row is not None
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (25, 5, 30)
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    logged = json.loads(error_records[0].getMessage())
    assert logged["event"] == "turn_failed"
    assert logged["exception_type"] == "InvalidToolArgumentsError"
    assert error_records[0].exc_info is not None
    assert error_records[0].exc_text is not None
    assert "InvalidToolArgumentsError" in error_records[0].exc_text


def test_receive_message_records_full_usage_when_the_tool_loop_limit_is_exceeded(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ToolLoopLimitError fires after all MAX_TOOL_ITERATIONS real model
    calls succeeded -- none of them ever discarded, unlike before this
    fix, where the loop's exhaustion raise carried no usage at all.
    turn_count must still increment too: a turn that burned real,
    already-paid-for model calls counts against CLAUDE.md §9's turn cap
    even though it never produced a sendable reply -- see
    caps.increment_turn_count's own docstring for why delivery is not
    the event that matters."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _ScriptedTransport(
        [
            _function_call_response(
                "check_availability",
                _HARMLESS_AVAILABILITY_ARGS,
                prompt_tokens=10,
                candidates_tokens=2,
            )
            for _ in range(MAX_TOOL_ITERATIONS)
        ]
    )
    _set_transport(monkeypatch, transport)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.tool-loop-limit", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "turn_failed"}
    assert len(transport.calls) == MAX_TOOL_ITERATIONS
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (
        10 * MAX_TOOL_ITERATIONS,
        2 * MAX_TOOL_ITERATIONS,
        12 * MAX_TOOL_ITERATIONS,
    )
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    logged = json.loads(error_records[0].getMessage())
    assert logged["event"] == "turn_failed"
    assert logged["exception_type"] == "ToolLoopLimitError"
    assert error_records[0].exc_info is not None
    assert error_records[0].exc_text is not None
    assert "ToolLoopLimitError" in error_records[0].exc_text
    turn_count_row = db_conn.execute(
        "SELECT turn_count FROM conversations WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert turn_count_row == (1,)


def test_receive_message_records_partial_usage_for_a_missing_price_rule_chain(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The first of three pricing exceptions dispatch.py deliberately
    lets propagate -- IncompletePriceRuleChainError, here, from no
    price_rule being configured at all. Tested separately from
    NoMatchingBandError and InconsistentPriceConfigurationError below
    even though dispatch.py's own docstring treats all three as one
    undifferentiated "business-data problem" category with no distinct
    handling anywhere in this codebase today: that shared-code-path
    reasoning is exactly the kind of thing a future change could
    invalidate for one of the three without anyone noticing, if only one
    of them had a test."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    seed_season(
        db_conn,
        season_name="Default",
        calendar_type="gregorian",
        start_month=1,
        start_day=1,
        end_month=1,
        end_day=1,
        priority=0,
        is_default=True,
    )
    # Far enough in the future that compute_quote's "check_in must not be
    # in the past" validation never trips no matter when this test runs.
    stay_check_in = date(2030, 1, 10)
    seed_allotment_nights(
        db_conn, hotel_id, room_type_id, stay_check_in, nights=1, total_rooms=5
    )
    # Deliberately no price_rule seeded.
    _set_llm_settings(monkeypatch, _settings())
    transport = _ScriptedTransport(
        [
            _function_call_response(
                "get_quote",
                {
                    "hotel_id": hotel_id,
                    "room_type_id": room_type_id,
                    "check_in": "2030-01-10",
                    "check_out": "2030-01-11",
                    "rooms": 1,
                },
                prompt_tokens=25,
                candidates_tokens=5,
            )
        ]
    )
    _set_transport(monkeypatch, transport)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.pricing-misconfig", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "turn_failed"}
    assert len(transport.calls) == 1
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (25, 5, 30)
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    logged = json.loads(error_records[0].getMessage())
    assert logged["event"] == "turn_failed"
    assert logged["exception_type"] == "IncompletePriceRuleChainError"
    assert error_records[0].exc_info is not None
    assert error_records[0].exc_text is not None
    assert "IncompletePriceRuleChainError" in error_records[0].exc_text


def test_receive_message_records_partial_usage_for_a_fully_booked_night(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The second pricing exception: NoMatchingBandError. A demand_curve's
    occupancy_bands top band is conventionally {"min": 0, "max": 1} --
    but lookup_band_value's range check is [min, max), so occupancy
    exactly 1.0 (a night reserved to full capacity) falls outside every
    band despite the config satisfying migration 0006's "full coverage"
    CHECK constraint. dispatch_get_quote only checks that an allotment
    row exists for the requested dates (services/agent/llm/dispatch.py's
    _allotment_covers_every_night), not whether the night still has room
    left -- a real customer can ask for a quote on a night that just
    became fully booked, so this is a genuine, reachable path, not a
    contrived one."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    seed_season(
        db_conn,
        season_name="Default",
        calendar_type="gregorian",
        start_month=1,
        start_day=1,
        end_month=1,
        end_day=1,
        priority=0,
        is_default=True,
    )
    stay_date = date(2030, 1, 10)
    # reserved == total: occupancy resolves to exactly 1.0, one past the
    # flat_demand_curve() occupancy band's exclusive upper bound of 1.
    seed_allotment_night(
        db_conn, hotel_id, room_type_id, stay_date, total_rooms=5, reserved=5
    )
    seed_price_rule(
        db_conn,
        scope="global",
        target_margin_bps=2_000,
        min_profit_by_lead_time=flat_min_profit(1_000),
        demand_curve=flat_demand_curve(),
    )
    _set_llm_settings(monkeypatch, _settings())
    transport = _ScriptedTransport(
        [
            _function_call_response(
                "get_quote",
                {
                    "hotel_id": hotel_id,
                    "room_type_id": room_type_id,
                    "check_in": "2030-01-10",
                    "check_out": "2030-01-11",
                    "rooms": 1,
                },
                prompt_tokens=25,
                candidates_tokens=5,
            )
        ]
    )
    _set_transport(monkeypatch, transport)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.fully-booked-night", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "turn_failed"}
    assert len(transport.calls) == 1
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (25, 5, 30)
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    logged = json.loads(error_records[0].getMessage())
    assert logged["event"] == "turn_failed"
    assert logged["exception_type"] == "NoMatchingBandError"
    assert error_records[0].exc_info is not None
    assert error_records[0].exc_text is not None
    assert "NoMatchingBandError" in error_records[0].exc_text


def test_receive_message_records_partial_usage_when_the_price_floor_exceeds_the_ask(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The third pricing exception: InconsistentPriceConfigurationError,
    from a price_rule whose margin is too thin to clear its own minimum
    profit floor -- a tiny target_margin_bps against a much larger flat
    min_profit_by_lead_time, so min_allowed (cost + min_profit) ends up
    above ask (cost marked up by the margin)."""
    hotel_id, room_type_id = seed_hotel_and_room_type(db_conn)
    seed_season(
        db_conn,
        season_name="Default",
        calendar_type="gregorian",
        start_month=1,
        start_day=1,
        end_month=1,
        end_day=1,
        priority=0,
        is_default=True,
    )
    stay_date = date(2030, 1, 10)
    # cost 10_000 halalas, unoccupied (occupancy 0, safely inside the
    # flat_demand_curve()'s [0, 1) band -- this test is not about
    # NoMatchingBandError).
    seed_allotment_nights(
        db_conn, hotel_id, room_type_id, stay_date, nights=1, total_rooms=5
    )
    seed_price_rule(
        db_conn,
        scope="global",
        # 1% margin: ask = 10_000 * 1.01 = 10_100 (demand_curve is a flat
        # 1.0x multiplier, so it does not change this).
        target_margin_bps=100,
        # min_allowed = 10_000 + 5_000 = 15_000, well above the 10_100 ask.
        min_profit_by_lead_time=flat_min_profit(5_000),
        demand_curve=flat_demand_curve(),
    )
    _set_llm_settings(monkeypatch, _settings())
    transport = _ScriptedTransport(
        [
            _function_call_response(
                "get_quote",
                {
                    "hotel_id": hotel_id,
                    "room_type_id": room_type_id,
                    "check_in": "2030-01-10",
                    "check_out": "2030-01-11",
                    "rooms": 1,
                },
                prompt_tokens=25,
                candidates_tokens=5,
            )
        ]
    )
    _set_transport(monkeypatch, transport)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.price-floor-exceeds-ask", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "turn_failed"}
    assert len(transport.calls) == 1
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (25, 5, 30)
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    logged = json.loads(error_records[0].getMessage())
    assert logged["event"] == "turn_failed"
    assert logged["exception_type"] == "InconsistentPriceConfigurationError"
    assert error_records[0].exc_info is not None
    assert error_records[0].exc_text is not None
    assert "InconsistentPriceConfigurationError" in error_records[0].exc_text


def test_receive_message_records_partial_usage_and_logs_a_never_seen_error(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Not one of generate_reply's own documented exception types at all
    -- a plain RuntimeError standing in for a genuine bug or a database
    outage inside the loop, the exact scenario the "always return 200"
    design must not quietly swallow. Proves the structural guarantee: the
    funnel does not need to know about this exception type in advance to
    record its prior usage and log it loudly."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _ScriptedTransport(
        [
            _function_call_response(
                "check_availability",
                _HARMLESS_AVAILABILITY_ARGS,
                prompt_tokens=25,
                candidates_tokens=5,
            ),
            RuntimeError("simulated bug or database outage"),
        ]
    )
    _set_transport(monkeypatch, transport)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.never-seen-error", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "turn_failed"}
    assert len(transport.calls) == 2
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (25, 5, 30)
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    logged = json.loads(error_records[0].getMessage())
    assert logged["event"] == "turn_failed"
    assert logged["exception_type"] == "RuntimeError"
    assert logged["exception_message"] == "simulated bug or database outage"
    assert error_records[0].exc_info is not None
    assert error_records[0].exc_info[0] is RuntimeError
    assert error_records[0].exc_info[2] is not None
    assert error_records[0].exc_text is not None
    assert "simulated bug or database outage" in error_records[0].exc_text


def test_receive_message_stores_message_but_skips_model_when_daily_rate_cap_exceeded(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    settings = _settings(max_messages_per_number_per_day=1)
    _set_llm_settings(monkeypatch, settings)
    transport = _FakeTransport()
    _set_transport(monkeypatch, transport)
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    # One inbound message already logged today, seeded directly — the
    # webhook itself never ran for it, so the fake transport's call count
    # below reflects only what happens to the *next* message.
    seed_message(
        db_conn,
        conversation_id,
        direction="inbound",
        body="earlier",
        customer_phone=_PHONE,
    )
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.rate-limited", body="one too many"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "rate_limited"}
    assert transport.calls == []
    row = db_conn.execute(
        "SELECT whatsapp_message_id FROM messages WHERE conversation_id = %s "
        "AND whatsapp_message_id = 'wamid.rate-limited'",
        (conversation_id,),
    ).fetchone()
    assert row is not None


def test_receive_message_second_message_hits_the_cap_from_the_first_recording(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    """Proves the counter is live, not just written: the first message's
    usage (60 tokens) is recorded by the webhook itself, and the second
    message's own pre-flight check sees that recorded total and blocks —
    without this second message ever calling the model. Also proves the
    second, capped message still gets escalated and answered with the
    fallback, not left silent."""
    settings = _settings(max_tokens_per_conversation=50)
    _set_llm_settings(monkeypatch, settings)
    transport = _FakeTransport(prompt_tokens=50, candidates_tokens=10)
    _set_transport(monkeypatch, transport)
    # Two distinct outbound sends happen across this test (the first
    # turn's real reply, the second turn's fallback) -- messages.
    # whatsapp_message_id is UNIQUE, so each needs its own fake sender
    # with a distinct message_id rather than reusing one instance.
    first_sender = _FakeWhatsAppSender(message_id="wamid.OUTBOUND-FIRST")
    _set_whatsapp_sender(monkeypatch, first_sender)

    first_payload = _whatsapp_payload(wa_id=_WA_ID, message_id="wamid.first", body="hi")
    first_response = _post(
        webhook_client,
        first_payload,
        signature=_sign(json.dumps(first_payload).encode()),
    )
    assert first_response.status_code == 200
    assert first_response.json() == {"status": "processed"}
    assert len(transport.calls) == 1

    second_sender = _FakeWhatsAppSender(message_id="wamid.OUTBOUND-SECOND")
    _set_whatsapp_sender(monkeypatch, second_sender)
    second_payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.second", body="are you there?"
    )
    second_response = _post(
        webhook_client,
        second_payload,
        signature=_sign(json.dumps(second_payload).encode()),
    )

    assert second_response.status_code == 200
    assert second_response.json() == {"status": "escalated"}
    # No second call: the first message's 60 recorded tokens already
    # exceed the 50-token cap before the second message's own model call
    # would have happened.
    assert len(transport.calls) == 1
    # 4: the first turn stores its inbound message and its outbound
    # reply; the second turn is capped before generate_reply ever
    # returns, but still stores its own inbound message plus the
    # fallback outbound reply sent by _escalate_cap_exceeded.
    assert _message_count(db_conn) == 4
    assert len(second_sender.calls) == 1
    assert second_sender.calls[0][1] == OUTPUT_GUARD_FALLBACK_MESSAGE
    escalation_row = db_conn.execute(
        "SELECT reason FROM escalations WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert escalation_row == ("token_spend_cap_exceeded",)


def test_receive_message_with_invalid_json_body_is_rejected(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport()
    _set_transport(monkeypatch, transport)
    body = b"not valid json"

    response = webhook_client.post(
        "/webhook/whatsapp",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(body),
        },
    )

    assert response.status_code == 400
    assert _message_count(db_conn) == 0
    assert transport.calls == []


def test_receive_message_ignores_a_status_callback_payload(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    """A delivery/read status callback has no "messages" key — a real
    event Meta sends to the same URL as inbound messages, not malformed
    input."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport()
    _set_transport(monkeypatch, transport)
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {"changes": [{"value": {"messaging_product": "whatsapp", "statuses": []}}]}
        ],
    }

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert _message_count(db_conn) == 0
    assert _conversation_count(db_conn) == 0
    assert transport.calls == []


def test_receive_message_duplicate_delivery_is_a_no_op(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport()
    _set_transport(monkeypatch, transport)
    payload = _whatsapp_payload(wa_id=_WA_ID, message_id="wamid.dup", body="hello")
    signature = _sign(json.dumps(payload).encode())

    first_response = _post(webhook_client, payload, signature=signature)
    assert first_response.status_code == 200
    assert first_response.json() == {"status": "processed"}
    assert len(transport.calls) == 1

    second_response = _post(webhook_client, payload, signature=signature)

    assert second_response.status_code == 200
    assert second_response.json() == {"status": "duplicate"}
    # No second model call, and no second inbound messages row for the
    # same WhatsApp message id — migration 0024's partial unique index
    # caught it. 2, not 1: the first (successful) turn stores both its
    # inbound message and its outbound reply.
    assert len(transport.calls) == 1
    assert _message_count(db_conn) == 2


def test_get_db_connection_opens_a_working_connection_and_closes_it(
    monkeypatch: pytest.MonkeyPatch, test_database_url: str
) -> None:
    """The one test that exercises the real get_db_connection — every
    other test in this file monkeypatches it to reuse the shared db_conn
    fixture connection instead, so this is what actually proves the
    production code path (open against DATABASE_URL, autocommit, close on
    exit) works, not just the test double standing in for it."""
    monkeypatch.setenv("DATABASE_URL", test_database_url)

    with webhook_module.get_db_connection() as conn:
        assert conn.autocommit is True
        row = conn.execute("SELECT 1").fetchone()
        assert row == (1,)

    assert conn.closed
