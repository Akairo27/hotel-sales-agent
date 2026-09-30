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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from services.agent import staff_follow_up as staff_follow_up_module
from services.agent import webhook as webhook_module
from services.agent.fixed_texts import FALLBACK, PLEASE_TYPE
from services.agent.llm import dispatch as dispatch_module
from services.agent.llm.caps import record_token_usage
from services.agent.llm.client import ModelTransport
from services.agent.llm.config import MAX_TOOL_ITERATIONS, LlmSettings
from services.agent.llm.conversation import UsageTotals
from services.agent.llm.dispatch import tool_error_result
from services.agent.llm.errors import (
    DailySpendCapExceededError,
    LlmConfigurationError,
    LlmError,
    ModelUnavailableError,
    NumberDailyTokenCapExceededError,
    TokenSpendCapExceededError,
    ToolLoopLimitError,
    TurnBudgetExceededError,
    TurnCapExceededError,
    UnknownToolError,
    UsageUnavailableError,
)
from services.agent.llm.model_types import (
    ModelResponse,
    ModelTurn,
    ModelUsage,
    ToolCall,
    ToolResultTurn,
    Turn,
    UserTurn,
)
from services.agent.llm.session import touch_last_message_at
from services.agent.main import app
from services.agent.output_guard.enforcement import (
    REASON_MISMATCH,
    GuardVerdict,
)
from services.agent.whatsapp_send import (
    WHATSAPP_TEXT_BODY_MAX_CHARS,
    WhatsAppSendConfigurationError,
    WhatsAppSender,
    WhatsAppSendError,
    WhatsAppSendSettings,
)
from services.inventory.operations import StayAvailability
from services.pricing.errors import PricingError
from tests.integration._seed import (
    flat_demand_curve,
    flat_min_profit,
    seed_allotment_night,
    seed_allotment_nights,
    seed_conversation,
    seed_escalation,
    seed_hotel,
    seed_message,
    seed_price_rule,
    seed_room_type,
    seed_season,
)

pytestmark = pytest.mark.usefixtures("db_conn")

_APP_SECRET = "test-app-secret"
_VERIFY_TOKEN = "test-verify-token"
_PHONE = "+966500000001"
_WA_ID = "966500000001"
_OTHER_WA_ID = "966500000002"

# A check_availability call that always resolves (to {"available": False},
# since _seed_searchable_hotel seeds its one night sold out) without
# raising -- used wherever a test needs a real, harmless tool call purely
# to keep generate_reply's loop going for another iteration. Sold out, not
# missing: a night with no inventory row is "not open for booking", which
# opens a staff follow-up escalation (services/agent/staff_follow_up.py) --
# a side effect a harmless call must not have. Its ids are only ever valid
# because every test that uses it seeds exactly one hotel/room type first
# (_seed_searchable_hotel) -- hotels/room_types are both RESTART
# IDENTITY-truncated before each test (tests/conftest.py), so that first
# insert is always id 1.
_HARMLESS_AVAILABILITY_ARGS = {
    "hotel_id": 1,
    "room_type_id": 1,
    # In the future: check_availability rejects a past check_in
    # (past_check_in) before reading any inventory.
    "check_in": "2031-01-01",
    "check_out": "2031-01-02",
    "rooms": 1,
}

_SEARCHABLE_HOTEL_NAME = "Test Hotel"

# The token cost of the search_hotels call every scripted transport below
# now has to make first, to satisfy dispatch_tool's resolved-stays guard
# (services/agent/llm/dispatch.py) before its first check_availability/
# get_quote call -- a fixed, named cost so each test's usage-total
# assertions can account for it explicitly rather than by a magic number.
_SEARCH_HOTELS_PROMPT_TOKENS = 25
_SEARCH_HOTELS_CANDIDATES_TOKENS = 5


def _seed_searchable_hotel(conn: psycopg.Connection[Any]) -> None:
    """A real, active, complete-profile hotel/room type search_hotels can
    resolve, matching _HARMLESS_AVAILABILITY_ARGS' hardcoded ids, with that
    stay's one night seeded sold out (see _HARMLESS_AVAILABILITY_ARGS). Must
    be the first hotels/room_types write in the calling test."""
    hotel_id = seed_hotel(
        conn,
        hotel_name=_SEARCHABLE_HOTEL_NAME,
        city="makkah",
        zone="makkah_central",
        star_rating=4,
        distance_to_haram_meters=350,
        address_text="Test address",
    )
    assert hotel_id == _HARMLESS_AVAILABILITY_ARGS["hotel_id"]
    room_type_id = seed_room_type(conn, hotel_id, room_type_name="Standard")
    assert room_type_id == _HARMLESS_AVAILABILITY_ARGS["room_type_id"]
    seed_allotment_night(
        conn,
        hotel_id,
        room_type_id,
        date.fromisoformat(str(_HARMLESS_AVAILABILITY_ARGS["check_in"])),
        total_rooms=1,
        reserved=1,
    )


def _search_hotels_prefix_call() -> ModelResponse:
    """The extra, real, billed search_hotels call every scripted
    transport below must now make before its first check_availability/
    get_quote call -- resolves _SEARCHABLE_HOTEL_NAME into
    _HARMLESS_AVAILABILITY_ARGS' ids via dispatch_tool for real."""
    return _function_call_response(
        "search_hotels",
        {"hotel_name": _SEARCHABLE_HOTEL_NAME},
        prompt_tokens=_SEARCH_HOTELS_PROMPT_TOKENS,
        candidates_tokens=_SEARCH_HOTELS_CANDIDATES_TOKENS,
    )


def _settings(
    *,
    max_tokens_per_conversation: int = 1_000_000,
    max_spend_per_day_usd: Decimal = Decimal("1000"),
    max_messages_per_number_per_day: int = 1_000,
    max_conversation_turns: int = 20,
    max_tokens_per_number_per_day: int = 10_000_000,
) -> LlmSettings:
    return LlmSettings(
        model="test-model-v1",
        api_key="test-key",
        timeout_ms=20_000,
        max_conversation_turns=max_conversation_turns,
        max_tokens_per_conversation=max_tokens_per_conversation,
        max_spend_per_day_usd=max_spend_per_day_usd,
        max_messages_per_number_per_day=max_messages_per_number_per_day,
        max_tokens_per_number_per_day=max_tokens_per_number_per_day,
    )


@dataclass
class _FakeTransport:
    """Always returns a plain text reply with the given token counts — no
    tool calls, so generate_reply returns after exactly one call."""

    prompt_tokens: int = 50
    candidates_tokens: int = 10
    calls: list[str] = field(default_factory=list)

    async def generate(
        self, *, turns: list[Turn], system_instruction: str, deadline: float
    ) -> ModelResponse:
        del turns, system_instruction, deadline
        self.calls.append("call")
        return ModelResponse(
            turn=ModelTurn(text="hello from the model", tool_calls=()),
            usage=ModelUsage(
                prompt_tokens=self.prompt_tokens,
                candidates_tokens=self.candidates_tokens,
                total_tokens=self.prompt_tokens + self.candidates_tokens,
            ),
        )


@dataclass
class _NoUsageTransport:
    """Raises UsageUnavailableError directly -- what a real GeminiTransport
    raises for a Gemini response with no usable usage data (see
    tests/unit/test_llm_client.py for that translation-level proof, and
    services.agent.llm.model_types.ModelUsage's own docstring for why the
    provider-neutral response type cannot represent "no usage" at all).
    This fake exists only to drive the webhook's own handling of that
    exception end to end through the real endpoint, not to re-prove the
    Gemini-specific translation that produces it in practice."""

    calls: list[str] = field(default_factory=list)

    async def generate(
        self, *, turns: list[Turn], system_instruction: str, deadline: float
    ) -> ModelResponse:
        del turns, system_instruction, deadline
        self.calls.append("call")
        raise UsageUnavailableError("model response carried no usage_metadata")


@dataclass
class _ToolCallingTransport:
    """Calls search_hotels once (to satisfy dispatch_tool's resolved-stays
    guard -- services/agent/llm/dispatch.py), then check_availability
    repeatedly with the same real, resolved ids -- the night is sold out
    (_seed_searchable_hotel) or has no inventory row in this module's
    fresh, truncated test schema, so dispatch_tool runs for real and
    check_availability returns {"available": False} rather than raising,
    and generate_reply's tool-calling loop keeps iterating. This
    drives several real model calls in one turn, so the mid-loop
    spend-cap recheck (conversation.py) can be exercised end to end
    through the real webhook against a real, non-mocked
    check_token_spend_caps -- not just at the wiring level
    (tests/unit/test_llm_conversation.py) or the caps.py-arithmetic level
    (tests/integration/test_llm_caps.py).

    hotel_id/room_type_id/hotel_name must be a real, active, complete-
    profile hotel and room type the caller already seeded (see
    tests/integration/_seed.py's seed_hotel/seed_room_type) -- search_hotels
    only ever resolves a real row, never these hardcoded values on their
    own.
    """

    prompt_tokens: int
    candidates_tokens: int
    hotel_id: int
    room_type_id: int
    hotel_name: str
    calls: list[str] = field(default_factory=list)

    async def generate(
        self, *, turns: list[Turn], system_instruction: str, deadline: float
    ) -> ModelResponse:
        del turns, system_instruction, deadline
        self.calls.append("call")
        if len(self.calls) == 1:
            tool_call = ToolCall(
                id="call_1",
                name="search_hotels",
                args={"hotel_name": self.hotel_name},
            )
        else:
            tool_call = ToolCall(
                id=f"call_{len(self.calls)}",
                name="check_availability",
                args={
                    "hotel_id": self.hotel_id,
                    "room_type_id": self.room_type_id,
                    "check_in": _HARMLESS_AVAILABILITY_ARGS["check_in"],
                    "check_out": _HARMLESS_AVAILABILITY_ARGS["check_out"],
                    "rooms": 1,
                },
            )
        return ModelResponse(
            turn=ModelTurn(text=None, tool_calls=(tool_call,)),
            usage=ModelUsage(
                prompt_tokens=self.prompt_tokens,
                candidates_tokens=self.candidates_tokens,
                total_tokens=self.prompt_tokens + self.candidates_tokens,
            ),
        )


def _function_call_response(
    name: str,
    args: dict[str, Any],
    *,
    prompt_tokens: int,
    candidates_tokens: int,
) -> ModelResponse:
    """One real, billed model call whose response is a tool call —
    dispatch_tool runs it for real against this module's fresh test
    schema (no monkeypatching), so a call to a bad tool name or with bad
    arguments raises the real UnknownToolError/InvalidToolArgumentsError,
    and a valid check_availability call against nonexistent inventory
    keeps generate_reply's loop going without raising anything."""
    return ModelResponse(
        turn=ModelTurn(
            text=None, tool_calls=(ToolCall(id="call_0", name=name, args=args),)
        ),
        usage=ModelUsage(
            prompt_tokens=prompt_tokens,
            candidates_tokens=candidates_tokens,
            total_tokens=prompt_tokens + candidates_tokens,
        ),
    )


@dataclass
class _ScriptedTransport:
    """Returns (or raises) each scripted item in order, one per call to
    generate() -- one reusable fake standing in for a bespoke dataclass
    per exception type under test. Each script item is either a real,
    billed response (a ModelResponse, whose usage is always counted by
    conversation.py before anything else happens with it) or an
    exception the transport layer itself raises directly
    (ModelUnavailableError, UsageUnavailableError, or a stand-in for a
    completely unanticipated failure) -- exceptions dispatch_tool raises
    instead (UnknownToolError, InvalidToolArgumentsError, pricing
    misconfigurations) are triggered by scripting a function-call
    response naming a bad tool or bad arguments, not by raising from
    here."""

    script: list[ModelResponse | BaseException]
    calls: list[str] = field(default_factory=list)
    turns_seen: list[list[Turn]] = field(default_factory=list)

    async def generate(
        self, *, turns: list[Turn], system_instruction: str, deadline: float
    ) -> ModelResponse:
        del system_instruction, deadline
        item = self.script[len(self.calls)]
        self.calls.append("call")
        self.turns_seen.append(list(turns))
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


@dataclass
class _FlakyWhatsAppSender:
    """Fails the first `failures` sends, then succeeds -- for a turn whose
    reply send fails but whose fallback send (the next attempt) goes
    through. Records every attempted send, failed or not, and hands out a
    distinct message id per success (messages.whatsapp_message_id is
    unique)."""

    failures: int
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def send_text(self, *, to_phone: str, body: str) -> str:
        self.calls.append((to_phone, body))
        if len(self.calls) <= self.failures:
            raise WhatsAppSendError("simulated API error")
        return f"wamid.OUTBOUND-FLAKY-{len(self.calls)}"


def _escalations(db_conn: psycopg.Connection[Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every escalation for _PHONE, oldest first, as (reason, parsed notes)."""
    rows = db_conn.execute(
        "SELECT reason, notes FROM escalations WHERE customer_phone = %s ORDER BY id",
        (_PHONE,),
    ).fetchall()
    return [(reason, json.loads(notes)) for reason, notes in rows]


def _outbound_bodies(db_conn: psycopg.Connection[Any]) -> list[str]:
    rows = db_conn.execute(
        "SELECT body FROM messages WHERE customer_phone = %s "
        "AND direction = 'outbound' ORDER BY id",
        (_PHONE,),
    ).fetchall()
    return [body for (body,) in rows]


def _error_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """Every ERROR record from services.agent.webhook, parsed."""
    return [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.levelno == logging.ERROR and r.name == "services.agent.webhook"
    ]


def _turn_status(caplog: pytest.LogCaptureFixture) -> str:
    """The status _generate_and_deliver_reply logged for the one turn under
    test. Needs caplog at INFO for services.agent.webhook."""
    (finished,) = [
        entry
        for entry in (
            json.loads(r.getMessage())
            for r in caplog.records
            if r.levelno == logging.INFO and r.name == "services.agent.webhook"
        )
        if entry.get("event") == "reply_turn_finished"
    ]
    status: str = finished["status"]
    return status


def _signature_rejections(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """Every webhook_signature_rejected WARNING, parsed."""
    return [
        entry
        for entry in (
            json.loads(r.getMessage())
            for r in caplog.records
            if r.levelno == logging.WARNING and r.name == "services.agent.webhook"
        )
        if entry.get("event") == "webhook_signature_rejected"
    ]


def _assert_fallback_sent_and_escalated(
    db_conn: psycopg.Connection[Any],
    sender: _FakeWhatsAppSender,
    *,
    reason: str,
    expected_text: str = FALLBACK.english,
) -> dict[str, Any]:
    """CLAUDE.md rule 12's invariant for a failed turn: the customer got
    exactly the fallback message (sent and recorded), and exactly one
    escalation was opened, for `reason`. Returns that escalation's notes.

    expected_text defaults to the English fallback: every test customer here
    writes in English, and the fallback goes out in the language of their
    latest written message (services/agent/fixed_texts.py)."""
    assert sender.calls == [(_WA_ID, expected_text)]
    assert _outbound_bodies(db_conn) == [expected_text]
    ((escalation_reason, notes),) = _escalations(db_conn)
    assert escalation_reason == reason
    return notes


@contextlib.contextmanager
def _agent_connection(dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """A fresh autocommit connection as hotel_agent, closed on exit — the same
    shape as the real get_db_connection, but as the least-privilege role the
    webhook process connects as in production (migration 0027) instead of the
    test's privileged connection."""
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(params=["privileged", "hotel_agent"])
def webhook_client(
    request: pytest.FixtureRequest,
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Wires the real app to a Postgres connection and a fixed webhook
    secret, via plain monkeypatch — this module never uses FastAPI's
    dependency_overrides, so this is the same mechanism the module itself is
    tested with everywhere else in this repo.

    Every test in this module runs twice. "privileged" hands the app the
    test's own full-privilege connection (how the whole module ran before
    migration 0027). "hotel_agent" hands it a fresh connection as the
    least-privilege role the webhook process really connects as, while
    seeding and assertions stay on the privileged db_conn — so any SQL the
    agent runs that its grants or RLS policies do not allow fails here, in
    CI, instead of on the first customer turn.

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
    assert response.json() == {"status": "accepted"}
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


def test_receive_message_converts_markdown_bold_before_sending(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    """to_whatsapp_formatting runs on the model's reply before it reaches
    the customer (services/agent/webhook.py's _process_turn) -- proven
    end to end through the real endpoint here, not just at the unit level
    (tests/unit/test_whatsapp_send.py). The stored outbound message body
    is also the converted text, not the model's raw Markdown."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _ScriptedTransport([_text_response("Sure, **no problem** at all")])
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    payload = _whatsapp_payload(wa_id=_WA_ID, message_id="wamid.1", body="hello")

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert sender.calls[0][1] == "Sure, *no problem* at all"
    row = db_conn.execute(
        "SELECT body FROM messages "
        "WHERE customer_phone = %s AND direction = 'outbound'",
        (_PHONE,),
    ).fetchone()
    assert row == ("Sure, *no problem* at all",)


def _text_response(
    text: str, *, prompt_tokens: int = 50, candidates_tokens: int = 10
) -> ModelResponse:
    return ModelResponse(
        turn=ModelTurn(text=text, tool_calls=()),
        usage=ModelUsage(
            prompt_tokens=prompt_tokens,
            candidates_tokens=candidates_tokens,
            total_tokens=prompt_tokens + candidates_tokens,
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
    assert response.json() == {"status": "accepted"}
    assert len(transport.calls) == 1
    # The fallback was sent, not the guard-violating text.
    assert len(sender.calls) == 1
    assert sender.calls[0][1] == FALLBACK.english
    outbound_row = db_conn.execute(
        "SELECT direction, body FROM messages "
        "WHERE customer_phone = %s AND direction = 'outbound'",
        (_PHONE,),
    ).fetchone()
    assert outbound_row == ("outbound", FALLBACK.english)
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


def test_receive_message_sends_the_fallback_and_escalates_when_the_reply_send_fails(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The guard allows this reply (no stated price at all); the send of
    the reply itself fails. Before CLAUDE.md rule 12 that ended the turn
    in silence with no escalation. Now the funnel opens a delivery_failed
    escalation and sends the fallback, which this time goes through.
    Usage from the already-successful model call is still recorded."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport(prompt_tokens=50, candidates_tokens=10)
    _set_transport(monkeypatch, transport)
    sender = _FlakyWhatsAppSender(failures=1)
    _set_whatsapp_sender(monkeypatch, sender)
    caplog.set_level(logging.INFO, logger="services.agent.webhook")
    payload = _whatsapp_payload(wa_id=_WA_ID, message_id="wamid.send-fails", body="hi")

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert sender.calls == [
        (_WA_ID, "hello from the model"),
        (_WA_ID, FALLBACK.english),
    ]
    assert _outbound_bodies(db_conn) == [FALLBACK.english]
    ((reason, notes),) = _escalations(db_conn)
    assert reason == "delivery_failed"
    assert notes == {}
    usage_row = db_conn.execute(
        "SELECT total_tokens FROM token_usage WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert usage_row == (60,)
    events = [entry["event"] for entry in _error_events(caplog)]
    assert events == ["whatsapp_send_failed", "conversation_escalated"]
    assert _turn_status(caplog) == "escalated"


def test_receive_message_reports_failed_unrecorded_when_both_halves_fail(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The escalation insert fails AND every send fails: nothing reached
    the customer and no escalation row exists. The status says exactly
    that -- the ERROR lines are the only trail -- instead of anything that
    sounds like success."""
    _set_llm_settings(monkeypatch, _settings(max_conversation_turns=1))
    seed_conversation(db_conn, customer_phone=_PHONE, turn_count=1)

    def _fail_insert(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("simulated escalation-insert database error")

    monkeypatch.setattr(webhook_module, "open_escalation", _fail_insert)
    _set_whatsapp_sender(
        monkeypatch, _FailingWhatsAppSender(WhatsAppSendError("simulated API error"))
    )
    caplog.set_level(logging.INFO, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.both-halves-fail", body="hi"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert _escalations(db_conn) == []
    assert _outbound_bodies(db_conn) == []
    assert [entry["event"] for entry in _error_events(caplog)] == [
        "conversation_escalation_failed",
        "whatsapp_send_failed",
    ]
    assert _turn_status(caplog) == "failed_unrecorded"


def test_receive_message_escalates_when_the_model_transport_cannot_be_built(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """get_model_transport runs before generate_reply, so a failure there
    escapes _process_turn entirely: the last-resort net
    (_escalate_unexpected_background_failure) still sends the fallback and
    opens an internal_error escalation on a fresh connection, and the turn
    still logs its status."""
    _set_llm_settings(monkeypatch, _settings())

    def _unbuildable(_settings: LlmSettings) -> ModelTransport:
        raise LlmConfigurationError("OPENROUTER_API_KEY is empty")

    monkeypatch.setattr(webhook_module, "get_model_transport", _unbuildable)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    caplog.set_level(logging.INFO, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.transport-unbuildable", body="hi"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    notes = _assert_fallback_sent_and_escalated(
        db_conn, sender, reason="internal_error"
    )
    assert notes == {"exception_type": "LlmConfigurationError"}
    assert [entry["event"] for entry in _error_events(caplog)] == [
        "background_reply_failed",
        "conversation_escalated",
    ]
    assert _turn_status(caplog) == "escalated"


def test_receive_message_escalates_even_when_the_fallback_send_fails_too(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every send fails (an expired token, say): the customer cannot be
    reached at all, so the escalation is what makes the silent customer
    visible to a human, and the turn is reported escalated_undelivered,
    never "escalated". No outbound row exists for a send that never
    happened."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport(prompt_tokens=50, candidates_tokens=10)
    _set_transport(monkeypatch, transport)
    _set_whatsapp_sender(
        monkeypatch, _FailingWhatsAppSender(WhatsAppSendError("simulated API error"))
    )
    caplog.set_level(logging.INFO, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.all-sends-fail", body="hi"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert _outbound_bodies(db_conn) == []
    ((reason, _notes),) = _escalations(db_conn)
    assert reason == "delivery_failed"
    events = [entry["event"] for entry in _error_events(caplog)]
    assert events == [
        "whatsapp_send_failed",
        "conversation_escalated",
        "whatsapp_send_failed",
    ]
    assert _turn_status(caplog) == "escalated_undelivered"


def test_receive_message_logs_a_blocked_fallback_if_it_ever_happens(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Structurally impossible for the real fallback text
    -- test_every_fixed_text_rendering_is_always_allowed
    (tests/integration/test_output_guard.py) proves that end to end
    against the real guard -- so this test forces the scenario directly
    by faking enforce_outbound_text's verdict, to prove webhook.py's own
    handling of the branch rather than re-proving the guard's own
    invariant. The blocked reply's escalation (111) is the one the funnel
    reports against: no second escalation is opened for a guard block."""
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
    assert response.json() == {"status": "accepted"}
    assert call_count["n"] == 2
    (logged,) = _error_events(caplog)
    assert logged["event"] == "conversation_escalation_fallback_blocked"
    assert logged["escalation_id"] == 111
    assert logged["fallback_escalation_id"] == 222


def test_receive_message_escalates_when_the_guard_check_itself_errors(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The output guard itself raising (a database error, say) must not
    end the turn in silence: a delivery_failed escalation is opened. The
    fallback cannot be sent -- rule 8 sends even the fallback through the
    same broken guard -- so the escalation is what makes this visible to
    a human. Usage from the already-successful model call is still
    recorded."""
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
    assert response.json() == {"status": "accepted"}
    usage_row = db_conn.execute(
        "SELECT total_tokens FROM token_usage WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert usage_row == (60,)
    assert _outbound_bodies(db_conn) == []
    assert _escalations(db_conn) == [
        ("delivery_failed", {"exception_type": "RuntimeError"})
    ]
    logged = _error_events(caplog)
    assert [entry["event"] for entry in logged] == [
        "reply_delivery_failed",
        "conversation_escalated",
        "fallback_delivery_failed",
    ]
    assert logged[0]["exception_type"] == "RuntimeError"
    assert logged[2]["exception_type"] == "RuntimeError"


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
    not retried -- and, since CLAUDE.md rule 12, the customer gets the
    fallback and a usage_unavailable escalation opens instead of
    silence."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _NoUsageTransport()
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(wa_id=_WA_ID, message_id="wamid.no-usage", body="hello")

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert len(transport.calls) == 1
    conversation_row = db_conn.execute(
        "SELECT id FROM conversations WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert conversation_row is not None
    usage_row = db_conn.execute(
        "SELECT count(*) FROM token_usage WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert usage_row == (0,)
    notes = _assert_fallback_sent_and_escalated(
        db_conn, sender, reason="usage_unavailable"
    )
    assert notes == {"exception_type": "UsageUnavailableError"}

    logged = _error_events(caplog)
    assert [entry["event"] for entry in logged] == [
        "usage_unavailable",
        "conversation_escalated",
    ]
    assert logged[0]["conversation_id"] == conversation_row[0]


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
    assert response.json() == {"status": "accepted"}
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
    assert response.json() == {"status": "accepted"}
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
    assert response.json() == {"status": "accepted"}
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
    caplog: pytest.LogCaptureFixture,
) -> None:
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport()
    _set_transport(monkeypatch, transport)
    payload = _whatsapp_payload(wa_id=_WA_ID, message_id="wamid.bad-sig", body="hello")

    signature = _sign(b"not-the-real-body")
    caplog.set_level(logging.WARNING, logger="services.agent.webhook")

    response = _post(webhook_client, payload, signature=signature)

    assert response.status_code == 401
    assert _message_count(db_conn) == 0
    assert _conversation_count(db_conn) == 0
    assert transport.calls == []
    assert _signature_rejections(caplog) == [
        {
            "event": "webhook_signature_rejected",
            "problem": "mismatch",
            "body_bytes": len(json.dumps(payload).encode()),
        }
    ]
    assert signature.removeprefix("sha256=") not in caplog.text
    assert _APP_SECRET not in caplog.text


def test_receive_message_with_missing_signature_is_rejected_with_no_trace(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport()
    _set_transport(monkeypatch, transport)
    payload = _whatsapp_payload(wa_id=_WA_ID, message_id="wamid.no-sig", body="hello")

    caplog.set_level(logging.WARNING, logger="services.agent.webhook")

    response = _post(webhook_client, payload, signature=None)

    assert response.status_code == 401
    assert _message_count(db_conn) == 0
    assert _conversation_count(db_conn) == 0
    assert transport.calls == []
    assert [e["problem"] for e in _signature_rejections(caplog)] == ["missing"]


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
    # The token cap counts the conversation's CURRENT session only
    # (caps.check_token_spend_caps), so this usage has to sit inside it: an
    # earlier message 10 minutes ago opens the session, and the usage is
    # recorded after that. Usage from before the session opened is proven
    # not to count in tests/integration/test_llm_caps.py.
    db_conn.execute(
        "INSERT INTO messages (conversation_id, customer_phone, direction, "
        "whatsapp_message_id, body, created_at) "
        "VALUES (%s, %s, 'inbound', 'wamid.earlier', 'earlier', %s)",
        (conversation_id, _PHONE, datetime.now(UTC) - timedelta(minutes=10)),
    )
    record_token_usage(
        db_conn,
        conversation_id=conversation_id,
        customer_phone=_PHONE,
        usage=UsageTotals(50, 0, 50),
        now=datetime.now(UTC) - timedelta(minutes=5),
    )
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.capped", body="hi again"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert transport.calls == []
    assert len(sender.calls) == 1
    assert sender.calls[0][1] == FALLBACK.english
    inbound_row = db_conn.execute(
        "SELECT whatsapp_message_id FROM messages "
        "WHERE conversation_id = %s AND direction = 'inbound' "
        "AND whatsapp_message_id = 'wamid.capped'",
        (conversation_id,),
    ).fetchone()
    assert inbound_row == ("wamid.capped",)
    outbound_row = db_conn.execute(
        "SELECT direction, body FROM messages "
        "WHERE conversation_id = %s AND direction = 'outbound'",
        (conversation_id,),
    ).fetchone()
    assert outbound_row == ("outbound", FALLBACK.english)
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
    assert response.json() == {"status": "accepted"}
    assert transport.calls == []
    assert len(sender.calls) == 1
    assert sender.calls[0][1] == FALLBACK.english
    outbound_row = db_conn.execute(
        "SELECT direction, body FROM messages "
        "WHERE conversation_id = %s AND direction = 'outbound'",
        (conversation_id,),
    ).fetchone()
    assert outbound_row == ("outbound", FALLBACK.english)
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


def test_receive_message_logs_a_blocked_turn_cap_fallback_if_it_ever_happens(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Structurally impossible for the real fallback text
    -- test_every_fixed_text_rendering_is_always_allowed
    (tests/integration/test_output_guard.py) proves that end to end --
    so this test forces the scenario directly by faking
    enforce_outbound_text's verdict, mirroring test_receive_message_
    logs_a_blocked_fallback_if_it_ever_happens for the guard path, to
    prove _escalate_and_notify's own handling of the branch."""
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
    assert response.json() == {"status": "accepted"}
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    logged = [json.loads(r.getMessage()) for r in error_records]
    events = [entry["event"] for entry in logged]
    assert "conversation_escalated" in events
    assert "conversation_escalation_fallback_blocked" in events
    assert all(entry["reason"] == "turn_cap_exceeded" for entry in logged)


def test_receive_message_still_sends_the_fallback_when_the_escalation_insert_fails(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The escalation and the fallback are attempted independently: a
    failure to open the escalation (a database error on that one INSERT)
    used to skip the fallback send entirely, leaving the customer in
    silence. Now the customer still gets the fallback, and the failed
    escalation is logged loudly (notified_no_escalation)."""
    settings = _settings(max_conversation_turns=1)
    _set_llm_settings(monkeypatch, settings)
    seed_conversation(db_conn, customer_phone=_PHONE, turn_count=1)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)

    def _fake_open_escalation(
        _conn: psycopg.Connection[Any], *, conversation_id: int, reason: str, notes: Any
    ) -> int:
        del conversation_id, reason, notes
        raise RuntimeError("simulated escalation-insert database error")

    monkeypatch.setattr(webhook_module, "open_escalation", _fake_open_escalation)
    caplog.set_level(logging.INFO, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.turn-cap-escalation-fails", body="hi"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert _escalations(db_conn) == []
    assert sender.calls == [(_WA_ID, FALLBACK.english)]
    assert _outbound_bodies(db_conn) == [FALLBACK.english]
    error_records = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and r.name == "services.agent.webhook"
    ]
    assert len(error_records) == 1
    logged = json.loads(error_records[0].getMessage())
    assert logged["event"] == "conversation_escalation_failed"
    assert logged["reason"] == "turn_cap_exceeded"
    assert logged["exception_type"] == "RuntimeError"
    assert error_records[0].exc_info is not None
    assert _turn_status(caplog) == "notified_no_escalation"


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
    assert response.json() == {"status": "accepted"}
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
    # Each real model call reports 30 tokens (the first is search_hotels,
    # resolving the id the rest of the calls use -- see
    # _ToolCallingTransport). The 50-token cap is still under after 1 call
    # (30) but crossed by the pre-check before a 3rd call would happen
    # (60 >= 50) — so exactly 2 model calls should happen, and the
    # recorded row should cover exactly those two.
    _seed_searchable_hotel(db_conn)
    settings = _settings(max_tokens_per_conversation=50)
    _set_llm_settings(monkeypatch, settings)
    transport = _ToolCallingTransport(
        prompt_tokens=25,
        candidates_tokens=5,
        hotel_id=1,
        room_type_id=1,
        hotel_name=_SEARCHABLE_HOTEL_NAME,
    )
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
    assert response.json() == {"status": "accepted"}
    assert len(transport.calls) == 2
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (50, 10, 60)
    assert len(sender.calls) == 1
    assert sender.calls[0][1] == FALLBACK.english
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
    # calls of 500 prompt tokens each (the first is search_hotels,
    # resolving the id the rest of the calls use -- see
    # _ToolCallingTransport) cross that cap exactly on the pre-check
    # before a 3rd call would happen.
    _seed_searchable_hotel(db_conn)
    settings = _settings(max_spend_per_day_usd=Decimal("0.00075"))
    _set_llm_settings(monkeypatch, settings)
    transport = _ToolCallingTransport(
        prompt_tokens=500,
        candidates_tokens=0,
        hotel_id=1,
        room_type_id=1,
        hotel_name=_SEARCHABLE_HOTEL_NAME,
    )
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
    assert response.json() == {"status": "accepted"}
    assert len(transport.calls) == 2
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (1000, 0, 1000)
    assert len(sender.calls) == 1
    assert sender.calls[0][1] == FALLBACK.english
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
    """UsageUnavailableError raised on the model call right after the
    turn's real tool calls is the gap the structural fix closes: those
    earlier calls' usage was already known, sitting in generate_reply's
    own accumulator, before this response came back unusable. Unlike the
    single-call case (test_receive_message_returns_200_and_logs_when_
    usage_is_unavailable above), that earlier usage must now be recorded,
    not silently discarded."""
    _seed_searchable_hotel(db_conn)
    _set_llm_settings(monkeypatch, _settings())
    # What a real GeminiTransport raises for a response with no usable
    # usage data (see tests/unit/test_llm_client.py for that translation-
    # level proof, and _NoUsageTransport's own docstring above) -- the
    # provider-neutral ModelResponse cannot represent "no usage" at all,
    # so the third scripted call raises directly instead of returning an
    # unusable response object.
    unusable_response = UsageUnavailableError(
        "model response carried no usage_metadata"
    )
    transport = _ScriptedTransport(
        [
            _search_hotels_prefix_call(),
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
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.usage-unavailable-mid-turn", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert len(transport.calls) == 3
    conversation_row = db_conn.execute(
        "SELECT id FROM conversations WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert conversation_row is not None
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (50, 10, 60)
    _assert_fallback_sent_and_escalated(db_conn, sender, reason="usage_unavailable")
    logged = _error_events(caplog)
    assert [entry["event"] for entry in logged] == [
        "usage_unavailable",
        "conversation_escalated",
    ]
    assert logged[0]["conversation_id"] == conversation_row[0]


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
    "turn_failed". The earlier calls' real usage must still be recorded."""
    _seed_searchable_hotel(db_conn)
    _set_llm_settings(monkeypatch, _settings())
    transport = _ScriptedTransport(
        [
            _search_hotels_prefix_call(),
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
    assert response.json() == {"status": "accepted"}
    assert len(transport.calls) == 3
    conversation_row = db_conn.execute(
        "SELECT id FROM conversations WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert conversation_row is not None
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (50, 10, 60)
    assert len(sender.calls) == 1
    assert sender.calls[0][1] == FALLBACK.english
    escalation_row = db_conn.execute(
        "SELECT reason FROM escalations WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert escalation_row == ("model_unavailable",)
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    events = [json.loads(r.getMessage())["event"] for r in error_records]
    assert "conversation_escalated" in events


def test_receive_message_escalates_and_notifies_when_the_transport_fails_first(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The mid-turn test above always has one real prior call; this
    covers the case it doesn't -- retries exhausted on the very first
    model call, with no usage recorded at all this turn. usage_so_far
    is zero here, not None: unlike TurnCapExceededError,
    ModelUnavailableError is raised inside conversation.py's
    tool-calling loop, so attach_usage_so_far always runs, just with a
    still-zero UsageTotals. Escalation and the fallback send must not
    depend on any prior successful call having happened -- the same
    standard test_receive_message_escalates_and_sends_fallback_when_
    the_turn_cap_is_exceeded already proves for a cap that also fires
    with no usage.

    Also proves the conversation_escalated log event carries the real
    exception type and message for ModelUnavailableError specifically --
    the underlying transport failure that produced the 2026-09-21
    escalations was otherwise invisible in the logs, only in
    escalations.notes."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _ScriptedTransport(
        [ModelUnavailableError("simulated transport failure")]
    )
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.transport-fails-first-call", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert len(transport.calls) == 1
    assert len(sender.calls) == 1
    assert sender.calls[0][1] == FALLBACK.english
    escalation_row = db_conn.execute(
        "SELECT reason FROM escalations WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert escalation_row == ("model_unavailable",)
    usage_row_count = db_conn.execute(
        "SELECT count(*) FROM token_usage WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert usage_row_count == (0,)
    escalated_records = [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.levelno == logging.ERROR
        and json.loads(r.getMessage())["event"] == "conversation_escalated"
    ]
    assert len(escalated_records) == 1
    assert escalated_records[0]["exception_type"] == "ModelUnavailableError"
    assert escalated_records[0]["exception_message"] == ("simulated transport failure")


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
    the gap; no second call is needed. Owner decision: an unknown tool
    stays a failed turn (fallback + unknown_tool escalation), not a tool
    error the model is asked to retry."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _ScriptedTransport(
        [
            _function_call_response(
                "not_a_real_tool", {}, prompt_tokens=25, candidates_tokens=5
            )
        ]
    )
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.unknown-tool", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
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
    _assert_fallback_sent_and_escalated(db_conn, sender, reason="unknown_tool")
    logged = _error_events(caplog)
    assert [entry["event"] for entry in logged] == [
        "turn_failed",
        "conversation_escalated",
    ]
    assert logged[0]["exception_type"] == "UnknownToolError"
    assert "exception_message" not in logged[0]
    assert "in dispatch_tool" in logged[0]["traceback"]


def _tool_results_seen_by_call(
    transport: _ScriptedTransport, call_index: int
) -> list[dict[str, Any]]:
    """The tool results the model was handed in its call_index-th call."""
    result_turn = transport.turns_seen[call_index][-1]
    assert isinstance(result_turn, ToolResultTurn)
    return [result.result for result in result_turn.results]


def test_receive_message_hands_invalid_tool_arguments_back_to_the_model(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """InvalidToolArgumentsError -- a non-integer hotel_id a real
    function-calling model can plausibly hallucinate -- no longer ends the
    turn in silence: the model is handed the fixed invalid_arguments tool
    error, answers the customer, and that answer is delivered. No
    escalation: nothing failed."""
    _set_llm_settings(monkeypatch, _settings())
    bad_args = dict(_HARMLESS_AVAILABILITY_ARGS, hotel_id="not-an-int")
    reply_text = "Which hotel would you like me to check?"
    transport = _ScriptedTransport(
        [
            _function_call_response(
                "check_availability", bad_args, prompt_tokens=25, candidates_tokens=5
            ),
            _text_response(reply_text, prompt_tokens=25, candidates_tokens=5),
        ]
    )
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.invalid-tool-args", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert len(transport.calls) == 2
    assert _tool_results_seen_by_call(transport, 1) == [
        tool_error_result("invalid_arguments")
    ]
    assert sender.calls == [(_WA_ID, reply_text)]
    assert _escalations(db_conn) == []
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (50, 10, 60)
    assert _error_events(caplog) == []


def test_receive_message_lets_the_model_resolve_an_unsearched_hotel_and_answer(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    """The model skips search_hotels and uses an id directly: the
    resolved-stays guard rejects it with the unresolved_stay tool error,
    the model then calls search_hotels, retries, and answers -- one turn,
    delivered, no escalation. That recovery takes four model calls."""
    assert MAX_TOOL_ITERATIONS >= 4, "the recovery below needs four model calls"
    _seed_searchable_hotel(db_conn)
    _set_llm_settings(monkeypatch, _settings())
    reply_text = "That room is not available for those dates, sorry."
    transport = _ScriptedTransport(
        [
            _function_call_response(
                "check_availability",
                _HARMLESS_AVAILABILITY_ARGS,
                prompt_tokens=25,
                candidates_tokens=5,
            ),
            _search_hotels_prefix_call(),
            _function_call_response(
                "check_availability",
                _HARMLESS_AVAILABILITY_ARGS,
                prompt_tokens=25,
                candidates_tokens=5,
            ),
            _text_response(reply_text, prompt_tokens=25, candidates_tokens=5),
        ]
    )
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.unresolved-then-searched", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert len(transport.calls) == 4
    assert _tool_results_seen_by_call(transport, 1) == [
        tool_error_result("unresolved_stay")
    ]
    assert _tool_results_seen_by_call(transport, 3)[0]["available"] is False
    assert sender.calls == [(_WA_ID, reply_text)]
    assert _escalations(db_conn) == []


def test_receive_message_completes_a_correction_path_that_needs_five_model_calls(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    """Two separate mistakes in one turn: an id search_hotels never
    returned (unresolved_stay), then, after the search, a malformed date
    (invalid_arguments). Recovering takes five model calls -- one more than
    the old MAX_TOOL_ITERATIONS of 4 allowed, which would have ended this
    turn in a tool_loop_limit_exceeded escalation. With the owner's limit
    of 6 the reply is delivered and nothing is escalated."""
    assert MAX_TOOL_ITERATIONS >= 5, "the recovery below needs five model calls"
    _seed_searchable_hotel(db_conn)
    _set_llm_settings(monkeypatch, _settings())
    malformed_date = dict(_HARMLESS_AVAILABILITY_ARGS, check_in="1 January")
    reply_text = "That room is not available for those dates, sorry."
    transport = _ScriptedTransport(
        [
            _function_call_response(
                "check_availability",
                _HARMLESS_AVAILABILITY_ARGS,
                prompt_tokens=25,
                candidates_tokens=5,
            ),
            _search_hotels_prefix_call(),
            _function_call_response(
                "check_availability",
                malformed_date,
                prompt_tokens=25,
                candidates_tokens=5,
            ),
            _function_call_response(
                "check_availability",
                _HARMLESS_AVAILABILITY_ARGS,
                prompt_tokens=25,
                candidates_tokens=5,
            ),
            _text_response(reply_text, prompt_tokens=25, candidates_tokens=5),
        ]
    )
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.five-call-correction", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert len(transport.calls) == 5
    assert _tool_results_seen_by_call(transport, 1) == [
        tool_error_result("unresolved_stay")
    ]
    assert _tool_results_seen_by_call(transport, 3) == [
        tool_error_result("invalid_arguments")
    ]
    assert _tool_results_seen_by_call(transport, 4)[0]["available"] is False
    assert sender.calls == [(_WA_ID, reply_text)]
    assert _escalations(db_conn) == []


def test_receive_message_escalates_when_the_model_never_fixes_its_arguments(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    """A model that keeps sending bad arguments is bounded by
    MAX_TOOL_ITERATIONS like any other: the turn ends in the funnel
    (fallback + tool_loop_limit_exceeded escalation), never in silence."""
    _set_llm_settings(monkeypatch, _settings())
    bad_args = dict(_HARMLESS_AVAILABILITY_ARGS, hotel_id="not-an-int")
    transport = _ScriptedTransport(
        [
            _function_call_response(
                "check_availability", bad_args, prompt_tokens=10, candidates_tokens=2
            )
            for _ in range(MAX_TOOL_ITERATIONS)
        ]
    )
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.never-fixes-args", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert len(transport.calls) == MAX_TOOL_ITERATIONS
    notes = _assert_fallback_sent_and_escalated(
        db_conn, sender, reason="tool_loop_limit_exceeded"
    )
    assert notes == {"exception_type": "ToolLoopLimitError"}


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
    the event that matters. The first call is search_hotels, resolving
    the id every later call reuses -- it still counts as one of the
    MAX_TOOL_ITERATIONS iterations, so the loop still exhausts after
    exactly that many calls. The turn then ends in the funnel: fallback
    plus a tool_loop_limit_exceeded escalation."""
    _seed_searchable_hotel(db_conn)
    _set_llm_settings(monkeypatch, _settings())
    # Every call costs 10/2 tokens here, including the first (search_hotels)
    # one -- unlike _search_hotels_prefix_call's fixed 25/5, so the uniform
    # per-call arithmetic below (10 * MAX_TOOL_ITERATIONS, ...) still holds.
    script: list[ModelResponse | BaseException] = [
        _function_call_response(
            "search_hotels",
            {"hotel_name": _SEARCHABLE_HOTEL_NAME},
            prompt_tokens=10,
            candidates_tokens=2,
        )
    ]
    script.extend(
        _function_call_response(
            "check_availability",
            _HARMLESS_AVAILABILITY_ARGS,
            prompt_tokens=10,
            candidates_tokens=2,
        )
        for _ in range(MAX_TOOL_ITERATIONS - 1)
    )
    transport = _ScriptedTransport(script)
    _set_transport(monkeypatch, transport)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.tool-loop-limit", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
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
    logged = _error_events(caplog)
    assert [entry["event"] for entry in logged] == [
        "turn_failed",
        "conversation_escalated",
    ]
    assert logged[0]["exception_type"] == "ToolLoopLimitError"
    assert "in generate_reply" in logged[0]["traceback"]
    assert logged[1]["reason"] == "tool_loop_limit_exceeded"
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
    InconsistentPriceConfigurationError below even though dispatch.py's own
    docstring treats all three as one undifferentiated "business-data
    problem" category with no distinct handling anywhere in this codebase
    today: that shared-code-path reasoning is exactly the kind of thing a
    future change could invalidate for one of the three without anyone
    noticing, if only one of them had a test. The third, NoMatchingBandError,
    has no end-to-end trigger left since the 2026-09-29 pricing fix (valid
    data never produces it); test_no_exception_type_ends_a_turn_in_silence
    covers it. Each ends in the funnel with reason pricing_error."""
    hotel_id = seed_hotel(
        db_conn,
        hotel_name=_SEARCHABLE_HOTEL_NAME,
        city="makkah",
        zone="makkah_central",
        star_rating=4,
        distance_to_haram_meters=350,
        address_text="Test address",
    )
    room_type_id = seed_room_type(db_conn, hotel_id, room_type_name="Standard")
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
            _search_hotels_prefix_call(),
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
            ),
        ]
    )
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.pricing-misconfig", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert len(transport.calls) == 2
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (50, 10, 60)
    notes = _assert_fallback_sent_and_escalated(db_conn, sender, reason="pricing_error")
    assert notes == {"exception_type": "IncompletePriceRuleChainError"}
    logged = _error_events(caplog)
    assert [entry["event"] for entry in logged] == [
        "turn_failed",
        "conversation_escalated",
    ]
    assert logged[0]["exception_type"] == "IncompletePriceRuleChainError"
    assert "exception_message" not in logged[0]


def _seed_fully_booked_priceable_night(
    db_conn: psycopg.Connection[Any],
) -> tuple[int, int]:
    """Everything get_quote needs to price 2030-01-10 -- searchable hotel,
    room type, season, price rule -- except a free room: reserved == total,
    so occupancy resolves to exactly 1.0 (the top of flat_demand_curve()'s
    single occupancy band, which closes at 1)."""
    hotel_id = seed_hotel(
        db_conn,
        hotel_name=_SEARCHABLE_HOTEL_NAME,
        city="makkah",
        zone="makkah_central",
        star_rating=4,
        distance_to_haram_meters=350,
        address_text="Test address",
    )
    room_type_id = seed_room_type(db_conn, hotel_id, room_type_name="Standard")
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
    seed_allotment_night(
        db_conn, hotel_id, room_type_id, date(2030, 1, 10), total_rooms=5, reserved=5
    )
    seed_price_rule(
        db_conn,
        scope="global",
        target_margin_bps=2_000,
        min_profit_by_lead_time=flat_min_profit(1_000),
        demand_curve=flat_demand_curve(),
    )
    return hotel_id, room_type_id


def test_receive_message_answers_normally_for_a_fully_booked_night(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A night booked to full capacity used to fail the whole turn with
    NoMatchingBandError -- the customer got nothing.
    dispatch_get_quote's availability gate now declines it as an ordinary
    unpriced result before any price is computed, the model's next call
    answers, and that reply is delivered. No `quotes` row is written."""
    hotel_id, room_type_id = _seed_fully_booked_priceable_night(db_conn)
    _set_llm_settings(monkeypatch, _settings())
    reply_text = "Sorry, that room is not available for those dates."
    transport = _ScriptedTransport(
        [
            _search_hotels_prefix_call(),
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
            ),
            _text_response(reply_text, prompt_tokens=25, candidates_tokens=5),
        ]
    )
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.fully-booked-answered", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert len(transport.calls) == 3
    assert sender.calls == [(_WA_ID, reply_text)]
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (75, 15, 90)
    assert [r for r in caplog.records if r.levelno == logging.ERROR] == []
    quote_count = db_conn.execute("SELECT count(*) FROM quotes").fetchone()
    assert quote_count == (0,)
    # Sold out is not "not open for booking": no staff follow-up.
    assert _escalations(db_conn) == []


def test_receive_message_quotes_normally_when_the_last_room_is_taken_mid_turn(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The race between dispatch_get_quote's availability gate and
    compute_quote's own occupancy read: the last room is taken after the
    gate said yes. Simulated by a gate that reports availability over data
    that is in fact fully booked. Until 2026-09-29 occupancy 1.0 fell
    outside every valid band, so this failed the turn with
    NoMatchingBandError (fallback + pricing_error escalation). The night is
    now priced at the top occupancy band and the reply delivered normally --
    a quote is not a sale: create_hold still refuses the stay
    (test_a_sold_out_night_is_priced_but_a_hold_on_it_is_refused, in
    tests/integration/test_pricing_compute.py). The webhook's contract for a
    real pricing exception stays covered by the
    InconsistentPriceConfigurationError tests below and by
    test_no_exception_type_ends_a_turn_in_silence."""
    monkeypatch.setattr(
        dispatch_module,
        "stay_availability",
        lambda *_a, **_k: StayAvailability((), ()),
    )
    hotel_id, room_type_id = _seed_fully_booked_priceable_night(db_conn)
    _set_llm_settings(monkeypatch, _settings())
    reply_text = "Here is the price for that night."
    transport = _ScriptedTransport(
        [
            _search_hotels_prefix_call(),
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
            ),
            _text_response(reply_text, prompt_tokens=25, candidates_tokens=5),
        ]
    )
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    caplog.set_level(logging.INFO, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.last-room-taken-mid-turn", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert len(transport.calls) == 3
    assert sender.calls == [(_WA_ID, reply_text)]
    assert _turn_status(caplog) == "processed"
    assert _escalations(db_conn) == []
    assert _error_events(caplog) == []
    recorded = db_conn.execute("SELECT nights FROM quotes").fetchall()
    assert len(recorded) == 1
    assert recorded[0][0][0]["occupancy"] == 1.0


# A stay whose two nights have no inventory row at all -- not open for
# booking yet (owner decision, 2026-09-29), for _seed_searchable_hotel's ids.
_NOT_OPEN_STAY_ARGS = {
    "hotel_id": 1,
    "room_type_id": 1,
    "check_in": "2030-02-01",
    "check_out": "2030-02-03",
    "rooms": 1,
}
_NOT_OPEN_NIGHTS = ["2030-02-01", "2030-02-02"]


def _not_open_for_booking_turn(reply_text: str) -> _ScriptedTransport:
    """search_hotels, then check_availability and get_quote for the same
    not-yet-open stay, then the model's reply."""
    return _ScriptedTransport(
        [
            _search_hotels_prefix_call(),
            _function_call_response(
                "check_availability",
                _NOT_OPEN_STAY_ARGS,
                prompt_tokens=25,
                candidates_tokens=5,
            ),
            _function_call_response(
                "get_quote", _NOT_OPEN_STAY_ARGS, prompt_tokens=25, candidates_tokens=5
            ),
            _text_response(reply_text, prompt_tokens=25, candidates_tokens=5),
        ]
    )


def _staff_follow_up_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == "services.agent.staff_follow_up"
    ]


def test_receive_message_opens_one_staff_follow_up_for_dates_not_open_for_booking(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nights with no inventory row are not open for booking yet: both tools
    hand the model those dates in nights_without_allotment (never as
    unavailable_nights, which would read as fully booked), the reply is
    delivered normally, and staff get exactly one escalation for the turn
    -- not one per tool call -- listing the nights."""
    _seed_searchable_hotel(db_conn)
    _set_llm_settings(monkeypatch, _settings())
    reply_text = "Those dates are not open for booking yet; a colleague will follow up."
    transport = _not_open_for_booking_turn(reply_text)
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    caplog.set_level(logging.INFO, logger="services.agent.webhook")
    caplog.set_level(logging.INFO, logger="services.agent.staff_follow_up")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.dates-not-open", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    (availability_result,) = _tool_results_seen_by_call(transport, 2)
    assert availability_result["available"] is False
    assert availability_result["unavailable_nights"] == []
    assert availability_result["nights_without_allotment"] == _NOT_OPEN_NIGHTS
    (quote_result,) = _tool_results_seen_by_call(transport, 3)
    assert quote_result["priced"] is False
    assert quote_result["reason"] == "no_allotment_for_dates"
    assert quote_result["nights_without_allotment"] == _NOT_OPEN_NIGHTS
    assert sender.calls == [(_WA_ID, reply_text)]
    assert _turn_status(caplog) == "processed"
    assert _escalations(db_conn) == [
        (
            "dates_not_open_for_booking",
            {"stays": [{"hotel_id": 1, "room_type_id": 1, "nights": _NOT_OPEN_NIGHTS}]},
        )
    ]
    (event,) = _staff_follow_up_events(caplog)
    assert event["event"] == "dates_not_open_escalated"
    assert event["night_count"] == 2


def test_receive_message_still_delivers_the_reply_when_the_staff_follow_up_fails(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The follow-up is for staff; a failed insert is logged at ERROR and
    never costs the customer the answer."""
    _seed_searchable_hotel(db_conn)
    _set_llm_settings(monkeypatch, _settings())
    reply_text = "Those dates are not open for booking yet; a colleague will follow up."
    _set_transport(monkeypatch, _not_open_for_booking_turn(reply_text))
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)

    def _fail_to_open(*_args: Any, **_kwargs: Any) -> int:
        raise psycopg.OperationalError("simulated connection failure")

    monkeypatch.setattr(staff_follow_up_module, "open_escalation", _fail_to_open)
    caplog.set_level(logging.INFO, logger="services.agent.webhook")
    caplog.set_level(logging.INFO, logger="services.agent.staff_follow_up")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.dates-not-open-insert-fails", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert sender.calls == [(_WA_ID, reply_text)]
    assert _turn_status(caplog) == "processed"
    assert _escalations(db_conn) == []
    (event,) = _staff_follow_up_events(caplog)
    assert event["event"] == "dates_not_open_escalation_failed"
    assert event["exception_type"] == "OperationalError"


# A cost no other value in these tests can coincide with, and the ask and
# floor it produces under the price rule below (1% margin, flat 1.0x demand,
# 5_000 minimum profit): InconsistentPriceConfigurationError's message
# carries all three.
_DISTINCTIVE_COST_HALALAS = 123_457
_DISTINCTIVE_ASK_HALALAS = 124_691  # 123_457 * 10_100 // 10_000
_DISTINCTIVE_FLOOR_HALALAS = 128_457  # 123_457 + 5_000


@pytest.mark.parametrize(
    "escalation_insert_fails", [False, True], ids=["escalated", "insert-fails"]
)
def test_receive_message_never_logs_or_stores_cost_for_a_price_floor_above_the_ask(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
    escalation_insert_fails: bool,
) -> None:
    """The third pricing exception: InconsistentPriceConfigurationError,
    from a price_rule whose margin is too thin to clear its own minimum
    profit floor -- a tiny target_margin_bps against a much larger flat
    min_profit_by_lead_time, so min_allowed (cost + min_profit) ends up
    above ask (cost marked up by the margin).

    Its message holds the cost, the ask and the floor. None of them may
    appear in any log line, rendered traceback included, or in
    escalations.notes (CLAUDE.md §8: never log cost). The insert-fails
    variant covers the subtle route: an exception raised while the pricing
    error is still being handled would carry it as __context__, and its
    rendered traceback would print the pricing message."""
    hotel_id = seed_hotel(
        db_conn,
        hotel_name=_SEARCHABLE_HOTEL_NAME,
        city="makkah",
        zone="makkah_central",
        star_rating=4,
        distance_to_haram_meters=350,
        address_text="Test address",
    )
    room_type_id = seed_room_type(db_conn, hotel_id, room_type_name="Standard")
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
    # Unoccupied (occupancy 0, inside flat_demand_curve()'s single band --
    # this test is not about occupancy).
    seed_allotment_nights(
        db_conn,
        hotel_id,
        room_type_id,
        stay_date,
        nights=1,
        total_rooms=5,
        cost_per_night=_DISTINCTIVE_COST_HALALAS,
    )
    seed_price_rule(
        db_conn,
        scope="global",
        # 1% margin (demand_curve is a flat 1.0x multiplier, so it does not
        # change the ask).
        target_margin_bps=100,
        # The floor ends up above the ask.
        min_profit_by_lead_time=flat_min_profit(5_000),
        demand_curve=flat_demand_curve(),
    )
    if escalation_insert_fails:

        def _fail_insert(*_args: Any, **_kwargs: Any) -> int:
            raise RuntimeError("simulated escalation-insert database error")

        monkeypatch.setattr(webhook_module, "open_escalation", _fail_insert)
    _set_llm_settings(monkeypatch, _settings())
    transport = _ScriptedTransport(
        [
            _search_hotels_prefix_call(),
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
            ),
        ]
    )
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    # Every logger, every level: the leak check below reads all of it.
    caplog.set_level(logging.DEBUG)
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.price-floor-exceeds-ask", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert len(transport.calls) == 2
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (50, 10, 60)
    assert sender.calls == [(_WA_ID, FALLBACK.english)]
    events = [entry["event"] for entry in _error_events(caplog)]
    if escalation_insert_fails:
        assert _escalations(db_conn) == []
        assert events == ["turn_failed", "conversation_escalation_failed"]
    else:
        assert _escalations(db_conn) == [
            ("pricing_error", {"exception_type": "InconsistentPriceConfigurationError"})
        ]
        assert events == ["turn_failed", "conversation_escalated"]
    stored_notes = db_conn.execute(
        "SELECT coalesce(string_agg(notes, ' '), '') FROM escalations"
    ).fetchone()
    assert stored_notes is not None
    everything_logged = caplog.text
    for secret in (
        _DISTINCTIVE_COST_HALALAS,
        _DISTINCTIVE_ASK_HALALAS,
        _DISTINCTIVE_FLOOR_HALALAS,
    ):
        assert str(secret) not in everything_logged
        assert str(secret) not in stored_notes[0]


def test_receive_message_escalates_and_logs_a_never_seen_error(
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
    record its prior usage, log it loudly (type and frames-only
    traceback -- an unvetted message is never logged), send the fallback
    and open an internal_error escalation."""
    _seed_searchable_hotel(db_conn)
    _set_llm_settings(monkeypatch, _settings())
    transport = _ScriptedTransport(
        [
            _search_hotels_prefix_call(),
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
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.never-seen-error", body="hello"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert len(transport.calls) == 3
    usage_row = db_conn.execute(
        "SELECT prompt_tokens, candidates_tokens, total_tokens FROM token_usage "
        "WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert usage_row == (50, 10, 60)
    notes = _assert_fallback_sent_and_escalated(
        db_conn, sender, reason="internal_error"
    )
    assert notes == {"exception_type": "RuntimeError"}
    logged = _error_events(caplog)
    assert [entry["event"] for entry in logged] == [
        "turn_failed",
        "conversation_escalated",
    ]
    assert logged[0]["exception_type"] == "RuntimeError"
    assert "exception_message" not in logged[0]
    # The frames locate the fault without the message: the scripted
    # transport's generate() is where this one was raised.
    assert ", in generate\n" in logged[0]["traceback"]
    assert "simulated bug or database outage" not in caplog.text


@pytest.mark.parametrize("blank_text", ["", "   \n\t "], ids=["empty", "whitespace"])
def test_receive_message_escalates_a_blank_model_reply(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
    blank_text: str,
) -> None:
    """A model reply with no text (or only whitespace) has nothing to
    send; before CLAUDE.md rule 12 the empty body went to WhatsApp and the
    customer got nothing. Now it is an empty_reply failure: fallback plus
    escalation."""
    _set_llm_settings(monkeypatch, _settings())
    _set_transport(monkeypatch, _ScriptedTransport([_text_response(blank_text)]))
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    payload = _whatsapp_payload(wa_id=_WA_ID, message_id="wamid.blank-reply", body="hi")

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    notes = _assert_fallback_sent_and_escalated(db_conn, sender, reason="empty_reply")
    assert notes == {}
    logged = _error_events(caplog)
    assert logged[0]["event"] == "reply_undeliverable"
    assert logged[0]["reason"] == "empty_reply"


def test_receive_message_escalates_a_reply_longer_than_whatsapp_accepts(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    """One character over the Cloud API's text.body limit is never sent
    (the API would reject it): reply_too_long, fallback plus escalation."""
    _set_llm_settings(monkeypatch, _settings())
    too_long = "a" * (WHATSAPP_TEXT_BODY_MAX_CHARS + 1)
    _set_transport(monkeypatch, _ScriptedTransport([_text_response(too_long)]))
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    payload = _whatsapp_payload(wa_id=_WA_ID, message_id="wamid.too-long", body="hi")

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    _assert_fallback_sent_and_escalated(db_conn, sender, reason="reply_too_long")


def test_receive_message_delivers_a_reply_of_exactly_the_maximum_length(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    """The boundary of the length rule: exactly the limit is sendable."""
    _set_llm_settings(monkeypatch, _settings())
    at_limit = "a" * WHATSAPP_TEXT_BODY_MAX_CHARS
    _set_transport(monkeypatch, _ScriptedTransport([_text_response(at_limit)]))
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    payload = _whatsapp_payload(wa_id=_WA_ID, message_id="wamid.at-limit", body="hi")

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert sender.calls == [(_WA_ID, at_limit)]
    assert _escalations(db_conn) == []


def test_receive_message_escalates_when_the_whatsapp_send_settings_are_missing(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing WHATSAPP_* settings make every send impossible, the
    fallback included: the delivery_failed escalation is the only trail,
    and the turn is reported escalated_undelivered."""
    _set_llm_settings(monkeypatch, _settings())
    _set_transport(monkeypatch, _FakeTransport(prompt_tokens=50, candidates_tokens=10))

    def _missing_settings() -> WhatsAppSendSettings:
        raise WhatsAppSendConfigurationError("WHATSAPP_ACCESS_TOKEN is not set")

    monkeypatch.setattr(webhook_module, "get_whatsapp_send_settings", _missing_settings)
    caplog.set_level(logging.INFO, logger="services.agent.webhook")
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.no-send-settings", body="hi"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert _outbound_bodies(db_conn) == []
    assert _escalations(db_conn) == [
        ("delivery_failed", {"exception_type": "WhatsAppSendConfigurationError"})
    ]
    assert [entry["event"] for entry in _error_events(caplog)] == [
        "reply_delivery_failed",
        "conversation_escalated",
        "fallback_delivery_failed",
    ]
    assert _turn_status(caplog) == "escalated_undelivered"


def _exception_family(base: type[Exception]) -> list[type[Exception]]:
    """base and every subclass of it, however deep."""
    family = [base]
    for subclass in base.__subclasses__():
        family.extend(_exception_family(subclass))
    return family


# An independent statement of the expected escalations.reason for each
# exception a turn can end on -- deliberately written out here rather than
# read from webhook.py, so a change to the mapping has to change this too.
_ANTICIPATED_STOP_REASONS: dict[type[Exception], str] = {
    TurnCapExceededError: "turn_cap_exceeded",
    TokenSpendCapExceededError: "token_spend_cap_exceeded",
    NumberDailyTokenCapExceededError: "number_daily_token_cap_exceeded",
    DailySpendCapExceededError: "daily_spend_cap_exceeded",
    ModelUnavailableError: "model_unavailable",
    TurnBudgetExceededError: "turn_budget_exceeded",
}
_OTHER_NAMED_REASONS: dict[type[Exception], str] = {
    UsageUnavailableError: "usage_unavailable",
    ToolLoopLimitError: "tool_loop_limit_exceeded",
    UnknownToolError: "unknown_tool",
}
_EVERY_TURN_EXCEPTION = sorted(
    {
        *_exception_family(LlmError),
        *_exception_family(PricingError),
        RuntimeError,
        psycopg.DataError,
    },
    key=lambda exception_type: exception_type.__name__,
)


def _expected_reason(exception_type: type[Exception]) -> str:
    if exception_type in _ANTICIPATED_STOP_REASONS:
        return _ANTICIPATED_STOP_REASONS[exception_type]
    if exception_type in _OTHER_NAMED_REASONS:
        return _OTHER_NAMED_REASONS[exception_type]
    if issubclass(exception_type, PricingError):
        return "pricing_error"
    return "internal_error"


@pytest.mark.parametrize(
    "exception_type", _EVERY_TURN_EXCEPTION, ids=lambda t: t.__name__
)
def test_no_exception_type_ends_a_turn_in_silence(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
    exception_type: type[Exception],
) -> None:
    """CLAUDE.md rule 12, table-driven: every exception generate_reply can
    end on -- every LlmError and services.pricing error (found by walking
    the class trees, so a new subclass is covered without touching this
    test), a bug (RuntimeError) and a database error (psycopg.DataError) --
    gets exactly one fallback message and exactly one escalation with its
    expected reason. Only the six anticipated stops may carry their
    message into escalations.notes; for every other type the message must
    not appear in the notes or in any log line."""
    sentinel = f"sentinel-detail-{exception_type.__name__}"
    _set_llm_settings(monkeypatch, _settings())
    _set_transport(monkeypatch, _ScriptedTransport([exception_type(sentinel)]))
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    caplog.set_level(logging.DEBUG)
    payload = _whatsapp_payload(
        wa_id=_WA_ID,
        message_id=f"wamid.no-silence-{exception_type.__name__}",
        body="hi",
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    notes = _assert_fallback_sent_and_escalated(
        db_conn, sender, reason=_expected_reason(exception_type)
    )
    if exception_type in _ANTICIPATED_STOP_REASONS:
        assert notes == {"exception_type": exception_type.__name__, "detail": sentinel}
    else:
        assert notes == {"exception_type": exception_type.__name__}
        assert sentinel not in caplog.text


def test_receive_message_answers_the_first_message_past_the_daily_rate_cap(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Owner decision B: the first message past the cap is stored, never
    reaches the model, and gets the fallback plus an escalation."""
    settings = _settings(max_messages_per_number_per_day=1)
    _set_llm_settings(monkeypatch, settings)
    transport = _FakeTransport()
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    # One inbound message already logged today, seeded directly -- the
    # webhook itself never ran for it, so the fake transport's call count
    # below reflects only what happens to the *next* message.
    seed_message(
        db_conn,
        conversation_id,
        direction="inbound",
        body="earlier",
        customer_phone=_PHONE,
    )
    caplog.set_level(logging.INFO, logger="services.agent.webhook")
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
    notes = _assert_fallback_sent_and_escalated(
        db_conn, sender, reason="message_rate_cap_exceeded"
    )
    assert notes == {}
    assert _turn_status(caplog) == "escalated"


def test_receive_message_stays_silent_for_later_messages_past_the_rate_cap(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    """Only the first blocked message of the day is answered: a customer
    who keeps writing past the cap must not get a fallback per message."""
    settings = _settings(max_messages_per_number_per_day=1)
    _set_llm_settings(monkeypatch, settings)
    transport = _FakeTransport()
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    for body in ("earlier", "the first one past the cap"):
        seed_message(
            db_conn,
            conversation_id,
            direction="inbound",
            body=body,
            customer_phone=_PHONE,
        )
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.rate-limited-again", body="still here"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "rate_limited"}
    assert transport.calls == []
    assert sender.calls == []
    assert _escalations(db_conn) == []
    assert _message_count(db_conn) == 3


def test_receive_message_still_answers_the_message_exactly_at_the_rate_cap(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap-th message itself is allowed (an off-by-one blocked it until
    2026-09-29)."""
    _set_llm_settings(monkeypatch, _settings(max_messages_per_number_per_day=1))
    transport = _FakeTransport()
    _set_transport(monkeypatch, transport)
    payload = _whatsapp_payload(wa_id=_WA_ID, message_id="wamid.at-cap", body="hi")

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert len(transport.calls) == 1


def _payload_with_messages(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """_whatsapp_payload's envelope around the given messages array."""
    payload = _whatsapp_payload(wa_id=_WA_ID, message_id="unused", body="unused")
    payload["entry"][0]["changes"][0]["value"]["messages"] = messages
    return payload


def _media_message(message_id: str, message_type: str) -> dict[str, Any]:
    return {
        "from": _WA_ID,
        "id": message_id,
        "timestamp": "1700000000",
        "type": message_type,
        message_type: {},
    }


def _text_message(message_id: str, body: str) -> dict[str, Any]:
    return {
        "from": _WA_ID,
        "id": message_id,
        "timestamp": "1700000000",
        "type": "text",
        "text": {"body": body},
    }


def _post_messages(client: TestClient, messages: list[dict[str, Any]]) -> Any:
    payload = _payload_with_messages(messages)
    return _post(client, payload, signature=_sign(json.dumps(payload).encode()))


def _inbound_bodies(db_conn: psycopg.Connection[Any]) -> list[str]:
    rows = db_conn.execute(
        "SELECT body FROM messages WHERE customer_phone = %s "
        "AND direction = 'inbound' ORDER BY id",
        (_PHONE,),
    ).fetchall()
    return [body for (body,) in rows]


@pytest.mark.parametrize("message_type", ["audio", "image"])
def test_receive_message_asks_the_customer_to_type_after_a_voice_note_or_image(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
    message_type: str,
) -> None:
    """Owner decision A: stored, never sent to the model, answered with
    fixed_texts.PLEASE_TYPE through the guard, and escalated for staff."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport()
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    caplog.set_level(logging.INFO, logger="services.agent.webhook")

    response = _post_messages(
        webhook_client, [_media_message("wamid.media", message_type)]
    )

    assert response.status_code == 200
    assert response.json() == {"status": "unsupported_type"}
    assert transport.calls == []
    assert _inbound_bodies(db_conn) == [f"[{message_type} message]"]
    # No written message yet, so Arabic then English.
    assert sender.calls == [(_WA_ID, PLEASE_TYPE.render(None))]
    assert _outbound_bodies(db_conn) == [PLEASE_TYPE.render(None)]
    assert _escalations(db_conn) == [
        ("unsupported_message_type", {"message_type": message_type})
    ]
    assert _turn_status(caplog) == "escalated"


@pytest.mark.parametrize(
    "message_type", ["video", "document", "location", "contacts", "interactive"]
)
def test_receive_message_answers_other_media_with_the_fallback(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    message_type: str,
) -> None:
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport()
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)

    response = _post_messages(
        webhook_client, [_media_message("wamid.media", message_type)]
    )

    assert response.status_code == 200
    assert response.json() == {"status": "unsupported_type"}
    assert transport.calls == []
    # No written message yet, so the fallback goes out Arabic then English.
    notes = _assert_fallback_sent_and_escalated(
        db_conn,
        sender,
        reason="unsupported_message_type",
        expected_text=FALLBACK.render(None),
    )
    assert notes == {"message_type": message_type}


@pytest.mark.parametrize(
    "message_type", ["reaction", "sticker", "unsupported", "a_future_type"]
)
def test_receive_message_ignores_reactions_stickers_and_unknown_types(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    message_type: str,
) -> None:
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport()
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)

    response = _post_messages(
        webhook_client, [_media_message("wamid.ignored", message_type)]
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert _message_count(db_conn) == 0
    assert _conversation_count(db_conn) == 0
    assert transport.calls == []
    assert sender.calls == []


def _seed_written_message(db_conn: psycopg.Connection[Any], body: str) -> None:
    """An earlier written message from _PHONE -- the one that decides the
    language of a fixed text sent afterwards."""
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    seed_message(
        db_conn,
        conversation_id,
        direction="inbound",
        body=body,
        customer_phone=_PHONE,
        created_at=datetime.now(UTC) - timedelta(minutes=1),
    )


def test_receive_message_asks_an_arabic_customer_to_type_in_arabic_only(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    """Owner decision (2026-09-30): once the customer has written, a fixed
    text goes out in their language alone -- Saudi dialect for Arabic."""
    _set_llm_settings(monkeypatch, _settings())
    _set_transport(monkeypatch, _FakeTransport())
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    _seed_written_message(db_conn, "السلام عليكم، أبغى غرفة")

    _post_messages(webhook_client, [_media_message("wamid.voice-ar", "audio")])

    assert sender.calls == [(_WA_ID, PLEASE_TYPE.arabic)]


def test_receive_message_sends_an_indonesian_customer_the_indonesian_fallback(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    """A failed turn after an Indonesian message: the fallback in Indonesian
    alone, never English (the client's customers write Arabic, English and
    Indonesian)."""
    _set_llm_settings(monkeypatch, _settings())
    _set_transport(
        monkeypatch, _ScriptedTransport([ModelUnavailableError("simulated outage")])
    )
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.indonesian", body="Halo, berapa harga kamar?"
    )

    _post(webhook_client, payload, signature=_sign(json.dumps(payload).encode()))

    _assert_fallback_sent_and_escalated(
        db_conn,
        sender,
        reason="model_unavailable",
        expected_text=FALLBACK.indonesian,
    )


def test_receive_message_falls_back_to_both_languages_when_the_lookup_fails(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The language read failing never blocks the notice: it goes out
    Arabic then English, and the failure is a warning in the log."""
    _set_llm_settings(monkeypatch, _settings())
    _set_transport(monkeypatch, _FakeTransport())
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    _seed_written_message(db_conn, "السلام عليكم، أبغى غرفة")

    def _lookup_fails(*_args: Any, **_kwargs: Any) -> str:
        raise psycopg.OperationalError("simulated read failure")

    monkeypatch.setattr(webhook_module, "customer_language", _lookup_fails)
    caplog.set_level(logging.WARNING, logger="services.agent.webhook")

    _post_messages(webhook_client, [_media_message("wamid.voice-lookup", "audio")])

    assert sender.calls == [(_WA_ID, PLEASE_TYPE.render(None))]
    warnings = [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "services.agent.webhook"
    ]
    assert [w["event"] for w in warnings] == ["notice_language_lookup_failed"]


def test_receive_message_processes_every_message_in_a_batch(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    """Before 2026-09-29 only the first message of a delivery was read and
    the rest were dropped without a trace."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport()
    _set_transport(monkeypatch, transport)
    sender = _FlakyWhatsAppSender(failures=0)
    _set_whatsapp_sender(monkeypatch, sender)

    response = _post_messages(
        webhook_client,
        [
            _text_message("wamid.batch-1", "first"),
            _text_message("wamid.batch-2", "second"),
            _media_message("wamid.batch-3", "reaction"),
        ],
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "batch",
        "results": ["accepted", "accepted", "ignored"],
    }
    assert _inbound_bodies(db_conn) == ["first", "second"]
    assert len(transport.calls) == 2
    assert _outbound_bodies(db_conn) == ["hello from the model"] * 2


def test_receive_message_retries_only_the_message_that_could_not_be_stored(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure before a message is stored answers 500 so Meta redelivers
    the batch -- while the message that was stored is still answered (the
    job scheduled for it runs on a 500 too) and comes back as a duplicate
    on the retry, which then stores and answers the other one."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport()
    _set_transport(monkeypatch, transport)
    sender = _FlakyWhatsAppSender(failures=0)
    _set_whatsapp_sender(monkeypatch, sender)
    real_insert = webhook_module._insert_inbound_message
    failed_once: list[str] = []

    def _fail_the_second_message_once(
        conn: psycopg.Connection[Any], **kwargs: Any
    ) -> int | None:
        if kwargs["whatsapp_message_id"] == "wamid.retry-2" and not failed_once:
            failed_once.append("failed")
            raise psycopg.OperationalError("simulated insert failure")
        return real_insert(conn, **kwargs)

    monkeypatch.setattr(
        webhook_module, "_insert_inbound_message", _fail_the_second_message_once
    )
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")
    messages = [
        _text_message("wamid.retry-1", "first"),
        _text_message("wamid.retry-2", "second"),
    ]

    first_response = _post_messages(webhook_client, messages)

    assert first_response.status_code == 500
    assert first_response.json() == {
        "status": "batch",
        "results": ["accepted", "not_stored"],
    }
    assert _inbound_bodies(db_conn) == ["first"]
    assert len(transport.calls) == 1
    assert [e["event"] for e in _error_events(caplog)] == ["inbound_message_not_stored"]

    retry_response = _post_messages(webhook_client, messages)

    assert retry_response.status_code == 200
    assert retry_response.json() == {
        "status": "batch",
        "results": ["duplicate", "accepted"],
    }
    assert _inbound_bodies(db_conn) == ["first", "second"]
    assert len(transport.calls) == 2


def test_receive_message_still_answers_when_touching_last_message_at_fails(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure after the message is stored must not become a 500: the
    retry would be dropped as a duplicate and the message lost for good."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport()
    _set_transport(monkeypatch, transport)
    calls: list[int] = []

    def _fail_the_first_touch(
        conn: psycopg.Connection[Any], *, conversation_id: int
    ) -> None:
        calls.append(conversation_id)
        if len(calls) == 1:
            raise psycopg.OperationalError("simulated connection failure")
        touch_last_message_at(conn, conversation_id=conversation_id)

    monkeypatch.setattr(webhook_module, "touch_last_message_at", _fail_the_first_touch)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")

    response = _post_messages(webhook_client, [_text_message("wamid.touch", "hi")])

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert len(transport.calls) == 1
    assert [e["event"] for e in _error_events(caplog)] == [
        "touch_last_message_at_failed"
    ]


def test_receive_message_lets_a_message_through_when_the_rate_cap_check_fails(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fails open: one message over the cap is a smaller harm than a
    message lost for good (see the test above)."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _FakeTransport()
    _set_transport(monkeypatch, transport)

    def _raise_db_error(_conn: psycopg.Connection[Any], **_kwargs: Any) -> None:
        raise psycopg.OperationalError("simulated connection failure")

    monkeypatch.setattr(webhook_module, "check_message_rate_cap", _raise_db_error)
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")

    response = _post_messages(webhook_client, [_text_message("wamid.cap-check", "hi")])

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    assert len(transport.calls) == 1
    assert [e["event"] for e in _error_events(caplog)] == [
        "message_rate_cap_check_failed"
    ]


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
    assert first_response.json() == {"status": "accepted"}
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
    assert second_response.json() == {"status": "accepted"}
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
    assert second_sender.calls[0][1] == FALLBACK.english
    escalation_row = db_conn.execute(
        "SELECT reason FROM escalations WHERE customer_phone = %s", (_PHONE,)
    ).fetchone()
    assert escalation_row == ("token_spend_cap_exceeded",)


def _info_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """Every INFO record from services.agent.webhook, parsed."""
    return [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.levelno == logging.INFO and r.name == "services.agent.webhook"
    ]


def _escalation_reasons(db_conn: psycopg.Connection[Any]) -> list[str]:
    return [reason for reason, _notes in _escalations(db_conn)]


def _post_text(client: TestClient, *, message_id: str, body: str) -> None:
    payload = _whatsapp_payload(wa_id=_WA_ID, message_id=message_id, body=body)
    response = _post(client, payload, signature=_sign(json.dumps(payload).encode()))
    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}


def test_receive_message_escalates_when_the_number_daily_token_cap_is_reached(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    """CLAUDE.md §9's per-number daily token cap, end to end: the first
    message's 60 tokens are recorded by the webhook itself, and the second
    message is stopped by the 50-token per-number cap before any model
    call, with the fallback and a number_daily_token_cap_exceeded
    escalation. The per-session cap stays far above, so the new cap is the
    only one that could have stopped it."""
    _set_llm_settings(monkeypatch, _settings(max_tokens_per_number_per_day=50))
    transport = _FakeTransport(prompt_tokens=50, candidates_tokens=10)
    _set_transport(monkeypatch, transport)
    _set_whatsapp_sender(
        monkeypatch, _FakeWhatsAppSender(message_id="wamid.OUTBOUND-FIRST")
    )
    _post_text(webhook_client, message_id="wamid.number-cap-first", body="hi")
    second_sender = _FakeWhatsAppSender(message_id="wamid.OUTBOUND-SECOND")
    _set_whatsapp_sender(monkeypatch, second_sender)

    _post_text(webhook_client, message_id="wamid.number-cap-second", body="hello?")

    assert len(transport.calls) == 1
    assert second_sender.calls == [(_WA_ID, FALLBACK.english)]
    assert _escalation_reasons(db_conn) == ["number_daily_token_cap_exceeded"]


def test_a_number_capped_again_the_same_day_gets_the_fallback_but_no_new_escalation(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Owner decision 2026-09-30 (ARCHITECTURE.md §7): one cap escalation
    per number per Asia/Riyadh day. The first capped message opens it; a
    later one the same day still gets the fallback (never silence), but no
    second escalation. Run as hotel_agent too (webhook_client), this also
    proves migration 0031's grant: a lookup the role could not run would
    fail and open a second escalation."""
    _set_llm_settings(monkeypatch, _settings(max_tokens_per_conversation=50))
    transport = _FakeTransport(prompt_tokens=50, candidates_tokens=10)
    _set_transport(monkeypatch, transport)
    caplog.set_level(logging.INFO, logger="services.agent.webhook")
    senders = [_FakeWhatsAppSender(message_id=f"wamid.OUTBOUND-{n}") for n in range(3)]
    bodies = ["hi", "are you there?", "hello?"]

    for index, (sender, body) in enumerate(zip(senders, bodies, strict=True)):
        _set_whatsapp_sender(monkeypatch, sender)
        _post_text(webhook_client, message_id=f"wamid.collapse-{index}", body=body)

    assert len(transport.calls) == 1
    assert senders[1].calls == [(_WA_ID, FALLBACK.english)]
    assert senders[2].calls == [(_WA_ID, FALLBACK.english)]
    assert _escalation_reasons(db_conn) == ["token_spend_cap_exceeded"]
    escalation_row = db_conn.execute(
        "SELECT id, conversation_id FROM escalations WHERE customer_phone = %s",
        (_PHONE,),
    ).fetchone()
    assert escalation_row is not None
    escalation_id, conversation_id = escalation_row
    reused = [
        event
        for event in _info_events(caplog)
        if event.get("event") == "cap_escalation_reused"
    ]
    assert reused == [
        {
            "event": "cap_escalation_reused",
            "conversation_id": conversation_id,
            "reason": "token_spend_cap_exceeded",
            "escalation_id": escalation_id,
        }
    ]
    assert all(
        event["event"] != "cap_escalation_lookup_failed"
        for event in _error_events(caplog)
    )


def test_a_different_cap_the_same_day_reuses_the_number_s_escalation(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The collapse spans all four caps: a number already escalated today
    for its token cap gets only the fallback when the turn cap stops it."""
    _set_llm_settings(monkeypatch, _settings(max_conversation_turns=3))
    transport = _FakeTransport()
    _set_transport(monkeypatch, transport)
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    caplog.set_level(logging.INFO, logger="services.agent.webhook")
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE, turn_count=3)
    seed_escalation(
        db_conn,
        conversation_id,
        reason="token_spend_cap_exceeded",
        customer_phone=_PHONE,
    )

    _post_text(webhook_client, message_id="wamid.turn-cap-after-token-cap", body="hi")

    assert transport.calls == []
    assert sender.calls == [(_WA_ID, FALLBACK.english)]
    assert _escalation_reasons(db_conn) == ["token_spend_cap_exceeded"]
    assert _turn_status(caplog) == "escalated"


def test_a_cap_escalation_from_an_earlier_day_or_for_another_reason_is_not_reused(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    """Only a cap escalation opened today counts: yesterday's belongs to
    another day, and today's escalation for a non-cap reason (here a model
    outage) is a different problem a human must still see this one next
    to."""
    _set_llm_settings(monkeypatch, _settings(max_conversation_turns=3))
    _set_transport(monkeypatch, _FakeTransport())
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE, turn_count=3)
    seed_escalation(
        db_conn,
        conversation_id,
        reason="turn_cap_exceeded",
        customer_phone=_PHONE,
        opened_at=datetime.now(UTC) - timedelta(days=2),
    )
    seed_escalation(
        db_conn, conversation_id, reason="model_unavailable", customer_phone=_PHONE
    )

    _post_text(webhook_client, message_id="wamid.turn-cap-new-day", body="hi")

    assert sender.calls == [(_WA_ID, FALLBACK.english)]
    assert _escalation_reasons(db_conn) == [
        "turn_cap_exceeded",
        "model_unavailable",
        "turn_cap_exceeded",
    ]


def test_a_failure_that_is_not_a_cap_is_escalated_even_after_a_cap_escalation(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    """The collapse is for the four caps only: a model outage on a number
    already escalated today for a cap still opens its own escalation."""
    _set_llm_settings(monkeypatch, _settings())
    _set_transport(monkeypatch, _ScriptedTransport([ModelUnavailableError("HTTP 503")]))
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    seed_escalation(
        db_conn, conversation_id, reason="turn_cap_exceeded", customer_phone=_PHONE
    )

    _post_text(webhook_client, message_id="wamid.outage-after-cap", body="hi")

    assert sender.calls == [(_WA_ID, FALLBACK.english)]
    assert _escalation_reasons(db_conn) == ["turn_cap_exceeded", "model_unavailable"]


def test_a_failed_cap_escalation_lookup_opens_a_new_escalation(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the lookup itself fails, the safe side is a duplicate
    escalation, never a missed one -- and the customer still gets the
    fallback. The failure is logged by exception type only."""
    _set_llm_settings(monkeypatch, _settings(max_conversation_turns=3))
    _set_transport(monkeypatch, _FakeTransport())
    sender = _FakeWhatsAppSender()
    _set_whatsapp_sender(monkeypatch, sender)
    caplog.set_level(logging.INFO, logger="services.agent.webhook")
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE, turn_count=3)
    seed_escalation(
        db_conn, conversation_id, reason="turn_cap_exceeded", customer_phone=_PHONE
    )

    def _fail_lookup(
        _conn: psycopg.Connection[Any],
        *,
        customer_phone: str,
        reasons: tuple[str, ...],
        now: datetime,
    ) -> int | None:
        del customer_phone, reasons, now
        raise psycopg.OperationalError("the lookup failed")

    monkeypatch.setattr(webhook_module, "find_todays_cap_escalation", _fail_lookup)

    _post_text(webhook_client, message_id="wamid.lookup-fails", body="hi")

    assert sender.calls == [(_WA_ID, FALLBACK.english)]
    assert _escalation_reasons(db_conn) == ["turn_cap_exceeded", "turn_cap_exceeded"]
    failures = [
        event
        for event in _error_events(caplog)
        if event["event"] == "cap_escalation_lookup_failed"
    ]
    assert failures == [
        {
            "event": "cap_escalation_lookup_failed",
            "conversation_id": conversation_id,
            "reason": "turn_cap_exceeded",
            "exception_type": "OperationalError",
        }
    ]


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
    assert first_response.json() == {"status": "accepted"}
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


@dataclass
class _RecordingTransport:
    """Records the turns every model call is given, to prove what the
    model can see; always answers with a plain text reply."""

    turns_seen: list[list[Turn]] = field(default_factory=list)

    async def generate(
        self, *, turns: list[Turn], system_instruction: str, deadline: float
    ) -> ModelResponse:
        del system_instruction, deadline
        self.turns_seen.append(list(turns))
        return ModelResponse(
            turn=ModelTurn(text="hello from the model", tool_calls=()),
            usage=ModelUsage(prompt_tokens=10, candidates_tokens=5, total_tokens=15),
        )


_EARLIER_QUESTION = "a room for 5-7 October"
_EARLIER_ANSWER = "Sure, for 5-7 October."


def _seed_earlier_exchange(
    db_conn: psycopg.Connection[Any], conversation_id: int, *, ago: timedelta
) -> None:
    then = datetime.now(UTC) - ago
    seed_message(
        db_conn,
        conversation_id,
        direction="inbound",
        body=_EARLIER_QUESTION,
        customer_phone=_PHONE,
        created_at=then,
    )
    seed_message(
        db_conn,
        conversation_id,
        direction="outbound",
        body=_EARLIER_ANSWER,
        customer_phone=_PHONE,
        created_at=then + timedelta(seconds=30),
    )


def _turn_count(db_conn: psycopg.Connection[Any], conversation_id: int) -> int:
    row = db_conn.execute(
        "SELECT turn_count FROM conversations WHERE id = %s", (conversation_id,)
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_a_message_after_an_idle_gap_starts_a_fresh_session(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    """A bare "hello" ten hours later must not show the model the earlier
    dates, and the turn counter starts over instead of carrying the whole
    history of this phone number."""
    _set_llm_settings(monkeypatch, _settings())
    transport = _RecordingTransport()
    _set_transport(monkeypatch, transport)
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE, turn_count=7)
    _seed_earlier_exchange(db_conn, conversation_id, ago=timedelta(hours=10))
    payload = _whatsapp_payload(wa_id=_WA_ID, message_id="wamid.fresh", body="hello")

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert transport.turns_seen == [[UserTurn("hello")]]
    # Reset to 0 as the message arrived, then this one turn counted.
    assert _turn_count(db_conn, conversation_id) == 1


def test_a_message_within_the_idle_gap_keeps_the_context_and_counters(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    _set_llm_settings(monkeypatch, _settings())
    transport = _RecordingTransport()
    _set_transport(monkeypatch, transport)
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE, turn_count=7)
    _seed_earlier_exchange(db_conn, conversation_id, ago=timedelta(hours=1))
    payload = _whatsapp_payload(
        wa_id=_WA_ID, message_id="wamid.continued", body="and how much?"
    )

    response = _post(
        webhook_client, payload, signature=_sign(json.dumps(payload).encode())
    )

    assert response.status_code == 200
    assert transport.turns_seen == [
        [
            UserTurn(_EARLIER_QUESTION),
            ModelTurn(text=_EARLIER_ANSWER, tool_calls=()),
            UserTurn("and how much?"),
        ]
    ]
    assert _turn_count(db_conn, conversation_id) == 8


def test_last_message_at_moves_with_the_messages_of_a_turn(
    webhook_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    db_conn: psycopg.Connection[Any],
) -> None:
    _set_llm_settings(monkeypatch, _settings())
    _set_transport(monkeypatch, _RecordingTransport())
    conversation_id = seed_conversation(db_conn, customer_phone=_PHONE)
    db_conn.execute(
        "UPDATE conversations SET last_message_at = %s WHERE id = %s",
        (datetime(2020, 1, 1, tzinfo=UTC), conversation_id),
    )
    payload = _whatsapp_payload(wa_id=_WA_ID, message_id="wamid.stamp", body="hi")

    _post(webhook_client, payload, signature=_sign(json.dumps(payload).encode()))

    row = db_conn.execute(
        "SELECT last_message_at FROM conversations WHERE id = %s", (conversation_id,)
    ).fetchone()
    assert row is not None
    assert abs(datetime.now(UTC) - row[0]) < timedelta(minutes=1)
