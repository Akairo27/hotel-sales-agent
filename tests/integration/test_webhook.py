"""Integration tests for services/agent/webhook.py against a real Postgres
instance — signature verification, idempotent inbound logging, and the two
CLAUDE.md §9 caps (per-conversation token spend, per-number-per-day message
rate). No output guard and no outbound WhatsApp send here: this webhook
deliberately stops before either (see webhook.py's own module docstring).

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
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from google.genai import types

from services.agent import webhook as webhook_module
from services.agent.llm.caps import record_token_usage
from services.agent.llm.client import ModelTransport
from services.agent.llm.config import LlmSettings
from services.agent.llm.conversation import UsageTotals
from services.agent.main import app
from tests.integration._seed import seed_conversation, seed_message

pytestmark = pytest.mark.usefixtures("db_conn")

_APP_SECRET = "test-app-secret"
_VERIFY_TOKEN = "test-verify-token"
_PHONE = "+966500000001"
_WA_ID = "966500000001"
_OTHER_WA_ID = "966500000002"


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


@pytest.fixture
def webhook_client(
    db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Wires the real app to the test's own Postgres connection and a
    fixed webhook secret, via plain monkeypatch — this module never uses
    FastAPI's dependency_overrides, so this is the same mechanism the
    module itself is tested with everywhere else in this repo."""
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
    yield TestClient(app)


def _set_llm_settings(monkeypatch: pytest.MonkeyPatch, settings: LlmSettings) -> None:
    monkeypatch.setattr(webhook_module, "get_llm_settings", lambda: settings)


def _set_transport(monkeypatch: pytest.MonkeyPatch, transport: ModelTransport) -> None:
    monkeypatch.setattr(
        webhook_module, "get_model_transport", lambda _settings: transport
    )


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
    row = db_conn.execute(
        "SELECT direction, body, whatsapp_message_id FROM messages "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert row == ("inbound", "hello", "wamid.1")
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (50, 10, 60)


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


def test_receive_message_stores_message_but_skips_model_when_conversation_cap_exceeded(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    settings = _settings(max_tokens_per_conversation=50)
    _set_llm_settings(monkeypatch, settings)
    transport = _FakeTransport()
    _set_transport(monkeypatch, transport)
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
    assert response.json() == {"status": "capped"}
    assert transport.calls == []
    row = db_conn.execute(
        "SELECT whatsapp_message_id FROM messages WHERE conversation_id = %s",
        (conversation_id,),
    ).fetchone()
    assert row == ("wamid.capped",)
    # The cap was already at its limit before this turn made any model
    # call at all (usage_so_far is zero) — nothing new to record, so the
    # only token_usage row is the one seeded directly above, not a second
    # one from this request.
    usage_row_count = db_conn.execute(
        "SELECT count(*) FROM token_usage WHERE conversation_id = %s",
        (conversation_id,),
    ).fetchone()
    assert usage_row_count == (1,)


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
    returning "capped" — proven here end to end, through the real
    endpoint and a real, non-mocked check_token_spend_caps, not just at
    the wiring level (tests/unit/test_llm_conversation.py) or the
    caps.py-arithmetic level (tests/integration/test_llm_caps.py)."""
    # Each real model call reports 30 tokens. The 50-token cap is still
    # under after 1 call (30) but crossed by the pre-check before a 3rd
    # call would happen (60 >= 50) — so exactly 2 model calls should
    # happen, and the recorded row should cover exactly those two.
    settings = _settings(max_tokens_per_conversation=50)
    _set_llm_settings(monkeypatch, settings)
    transport = _ToolCallingTransport(prompt_tokens=25, candidates_tokens=5)
    _set_transport(monkeypatch, transport)
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.crosses-mid-turn", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "capped"}
    assert len(transport.calls) == 2
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (50, 10, 60)


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
    without this second message ever calling the model."""
    settings = _settings(max_tokens_per_conversation=50)
    _set_llm_settings(monkeypatch, settings)
    transport = _FakeTransport(prompt_tokens=50, candidates_tokens=10)
    _set_transport(monkeypatch, transport)

    first_payload = _whatsapp_payload(wa_id=_WA_ID, message_id="wamid.first", body="hi")
    first_response = _post(
        webhook_client,
        first_payload,
        signature=_sign(json.dumps(first_payload).encode()),
    )
    assert first_response.status_code == 200
    assert first_response.json() == {"status": "processed"}
    assert len(transport.calls) == 1

    second_payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.second", body="are you there?"
    )
    second_response = _post(
        webhook_client,
        second_payload,
        signature=_sign(json.dumps(second_payload).encode()),
    )

    assert second_response.status_code == 200
    assert second_response.json() == {"status": "capped"}
    # No second call: the first message's 60 recorded tokens already
    # exceed the 50-token cap before the second message's own model call
    # would have happened.
    assert len(transport.calls) == 1
    assert _message_count(db_conn) == 2


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
    # No second model call, and no second messages row for the same
    # WhatsApp message id — migration 0024's partial unique index caught it.
    assert len(transport.calls) == 1
    assert _message_count(db_conn) == 1


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
