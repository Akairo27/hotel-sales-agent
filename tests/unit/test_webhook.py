"""Unit tests for services/agent/webhook.py's pure helpers — no database,
no network. _signature_problem, _parse_inbound_messages, and
load_webhook_settings each have their own dedicated tests here rather than
being exercised only incidentally through the integration tests, matching
this repo's usual layering (e.g. output_guard/extraction.py vs.
enforcement.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast

import psycopg
import pytest

from services.agent import webhook as webhook_module
from services.agent.booking_buttons import ButtonTap, booking_offer_buttons
from services.agent.fixed_texts import FALLBACK
from services.agent.llm.client import GeminiTransport, OpenRouterTransport
from services.agent.llm.config import LlmSettings, OpenRouterRoute, load_llm_settings
from services.agent.llm.errors import LlmConfigurationError
from services.agent.llm.pricing import TokenRates
from services.agent.output_guard.enforcement import (
    GuardVerdict,
)
from services.agent.webhook import (
    WebhookConfigurationError,
    _funnel_status,
    _normalize_phone,
    _parse_inbound_messages,
    _signature_problem,
    get_llm_settings,
    get_model_transport,
    get_webhook_settings,
    load_webhook_settings,
)
from services.agent.whatsapp_send import (
    ReplyButton,
    WhatsAppMessageRejectedError,
    WhatsAppSendError,
)

_SECRET = "shared-secret"


def _signature_for(body: bytes, secret: str = _SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_signature_problem_is_none_for_a_correctly_signed_body() -> None:
    body = b'{"hello": "world"}'
    assert (
        _signature_problem(
            body=body, signature_header=_signature_for(body), app_secret=_SECRET
        )
        is None
    )


def test_signature_problem_is_a_mismatch_for_a_different_secret() -> None:
    body = b'{"hello": "world"}'
    wrong_signature = _signature_for(body, secret="a-different-secret")
    assert (
        _signature_problem(
            body=body, signature_header=wrong_signature, app_secret=_SECRET
        )
        == "mismatch"
    )


def test_signature_problem_is_a_mismatch_for_a_different_body() -> None:
    signed_for_other_body = _signature_for(b'{"other": "body"}')
    assert (
        _signature_problem(
            body=b'{"hello": "world"}',
            signature_header=signed_for_other_body,
            app_secret=_SECRET,
        )
        == "mismatch"
    )


def test_signature_problem_reports_a_missing_header() -> None:
    assert (
        _signature_problem(body=b"anything", signature_header=None, app_secret=_SECRET)
        == "missing"
    )


def test_signature_problem_reports_a_header_without_the_sha256_prefix() -> None:
    body = b'{"hello": "world"}'
    bare_digest = hmac.new(_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    assert (
        _signature_problem(body=body, signature_header=bare_digest, app_secret=_SECRET)
        == "malformed"
    )


def test_normalize_phone_adds_a_leading_plus_when_absent() -> None:
    assert _normalize_phone("966500000001") == "+966500000001"


def test_normalize_phone_leaves_an_existing_leading_plus_alone() -> None:
    assert _normalize_phone("+966500000001") == "+966500000001"


def _payload(**value_overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "messaging_product": "whatsapp",
        "contacts": [{"profile": {"name": "Test Customer"}, "wa_id": "966500000001"}],
        "messages": [
            {
                "from": "966500000001",
                "id": "wamid.1",
                "text": {"body": "hello"},
                "type": "text",
            }
        ],
    }
    value.update(value_overrides)
    return {"entry": [{"changes": [{"value": value}]}]}


def test_parse_inbound_messages_extracts_phone_name_id_type_and_body() -> None:
    (inbound,) = _parse_inbound_messages(_payload())

    assert inbound.customer_phone == "+966500000001"
    assert inbound.customer_name == "Test Customer"
    assert inbound.whatsapp_message_id == "wamid.1"
    assert inbound.message_type == "text"
    assert inbound.body == "hello"


def test_parse_inbound_messages_returns_nothing_when_there_are_no_messages() -> None:
    assert _parse_inbound_messages(_payload(messages=[])) == []


def test_parse_inbound_messages_returns_nothing_for_a_status_callback() -> None:
    """A delivery/read status callback has no "messages" key at all —
    Meta sends these to the same URL as inbound messages."""
    payload = {
        "entry": [
            {"changes": [{"value": {"messaging_product": "whatsapp", "statuses": []}}]}
        ]
    }
    assert _parse_inbound_messages(payload) == []


def test_parse_inbound_messages_keeps_a_media_message_with_a_placeholder_body() -> None:
    """Which types get answered is receive_message's decision; the parser
    keeps every well-formed message, with a placeholder body (plus the
    caption, when there is one) standing in for the media."""
    payload = _payload(
        messages=[
            {"from": "966500000001", "id": "wamid.2", "type": "audio", "audio": {}},
            {
                "from": "966500000001",
                "id": "wamid.3",
                "type": "image",
                "image": {"caption": "is this room free?"},
            },
            {"from": "966500000001", "id": "wamid.4", "type": "reaction"},
        ]
    )

    parsed = _parse_inbound_messages(payload)

    assert [(m.message_type, m.body) for m in parsed] == [
        ("audio", "[audio message]"),
        ("image", "[image message] is this room free?"),
        ("reaction", "[reaction message]"),
    ]


def test_parse_inbound_messages_returns_every_message_in_every_entry_and_change() -> (
    None
):
    """A delivery can batch several messages -- all of them are returned,
    in order, not just the first; each sender's display name comes from
    the contact whose wa_id matches."""
    second_value = {
        "contacts": [{"profile": {"name": "Other"}, "wa_id": "966500000002"}],
        "messages": [
            {
                "from": "966500000002",
                "id": "wamid.b",
                "type": "text",
                "text": {"body": "second"},
            }
        ],
    }
    payload = _payload(
        messages=[
            {
                "from": "966500000001",
                "id": "wamid.a1",
                "type": "text",
                "text": {"body": "first"},
            },
            {
                "from": "966500000001",
                "id": "wamid.a2",
                "type": "text",
                "text": {"body": "again"},
            },
        ]
    )
    entries = payload["entry"]
    assert isinstance(entries, list)
    entries.append({"changes": [{"value": second_value}]})

    parsed = _parse_inbound_messages(payload)

    assert [(m.whatsapp_message_id, m.customer_name, m.body) for m in parsed] == [
        ("wamid.a1", "Test Customer", "first"),
        ("wamid.a2", "Test Customer", "again"),
        ("wamid.b", "Other", "second"),
    ]


def test_parse_inbound_messages_skips_a_malformed_message_but_keeps_the_rest() -> None:
    payload = _payload(
        messages=[
            {"from": "966500000001", "type": "text", "text": {"body": "no id"}},
            {"from": "966500000001", "id": "wamid.x", "type": "text"},
            {
                "from": "966500000001",
                "id": "wamid.ok",
                "type": "text",
                "text": {"body": "fine"},
            },
        ]
    )

    parsed = _parse_inbound_messages(payload)

    assert [m.whatsapp_message_id for m in parsed] == ["wamid.ok"]


def test_parse_inbound_messages_falls_back_to_the_only_contact_without_from() -> None:
    payload = _payload(
        messages=[{"id": "wamid.1", "type": "text", "text": {"body": "hello"}}]
    )
    (inbound,) = _parse_inbound_messages(payload)

    assert inbound.customer_phone == "+966500000001"


def test_parse_inbound_messages_has_no_name_without_a_matching_contact() -> None:
    payload = _payload(contacts=[])
    (inbound,) = _parse_inbound_messages(payload)

    assert inbound.customer_phone == "+966500000001"
    assert inbound.customer_name is None


def test_parse_inbound_messages_returns_nothing_for_a_malformed_payload() -> None:
    assert _parse_inbound_messages({"entry": []}) == []
    assert _parse_inbound_messages({}) == []
    assert _parse_inbound_messages({"entry": "not-a-list"}) == []


def _interactive_message(
    interactive: dict[str, object], *, context: dict[str, object] | None = None
) -> dict[str, object]:
    message: dict[str, object] = {
        "from": "966500000001",
        "id": "wamid.tap",
        "type": "interactive",
        "interactive": interactive,
    }
    if context is not None:
        message["context"] = context
    return message


def test_a_tapped_reply_button_becomes_a_button_reply_titled_body() -> None:
    """Meta's shape for a reply-button tap: interactive.button_reply's id
    and title, and context.id -- the message that carried the button."""
    payload = _payload(
        messages=[
            _interactive_message(
                {
                    "type": "button_reply",
                    "button_reply": {"id": "booking:yes:7", "title": "Yes, confirm"},
                },
                context={"from": "15550000000", "id": "wamid.OFFER"},
            )
        ]
    )

    (inbound,) = _parse_inbound_messages(payload)

    assert inbound.message_type == "button_reply"
    assert inbound.body == "Yes, confirm"
    assert inbound.button == ButtonTap(
        button_id="booking:yes:7",
        title="Yes, confirm",
        context_message_id="wamid.OFFER",
    )


def test_a_button_reply_without_context_keeps_the_tap_with_no_context() -> None:
    payload = _payload(
        messages=[
            _interactive_message(
                {
                    "type": "button_reply",
                    "button_reply": {"id": "booking:yes:7", "title": "Yes, confirm"},
                }
            )
        ]
    )

    (inbound,) = _parse_inbound_messages(payload)

    assert inbound.button is not None
    assert inbound.button.context_message_id is None


@pytest.mark.parametrize(
    "interactive",
    [
        pytest.param(
            {"type": "list_reply", "list_reply": {"id": "x", "title": "Row"}},
            id="list-reply",
        ),
        pytest.param(
            {"type": "button_reply", "button_reply": {"title": "Yes, confirm"}},
            id="no-id",
        ),
        pytest.param(
            {"type": "button_reply", "button_reply": {"id": "x", "title": "  "}},
            id="blank-title",
        ),
        pytest.param({"type": "button_reply"}, id="no-button-reply"),
    ],
)
def test_any_other_interactive_message_stays_interactive(
    interactive: dict[str, object],
) -> None:
    """It keeps the fallback and an escalation, never silence."""
    (inbound,) = _parse_inbound_messages(
        _payload(messages=[_interactive_message(interactive)])
    )

    assert (inbound.message_type, inbound.body, inbound.button) == (
        "interactive",
        "[interactive message]",
        None,
    )


@dataclass
class _ButtonSender:
    """Records every send; the button send raises buttons_error when set."""

    buttons_error: Exception | None = None
    sends: list[tuple[str, str]] = field(default_factory=list)

    async def send_text(self, *, to_phone: str, body: str) -> str:
        del to_phone
        self.sends.append(("text", body))
        return "wamid.TEXT"

    async def send_reply_buttons(
        self, *, to_phone: str, body: str, buttons: tuple[ReplyButton, ...]
    ) -> str:
        del to_phone, buttons
        self.sends.append(("buttons", body))
        if self.buttons_error is not None:
            raise self.buttons_error
        return "wamid.BUTTONS"


def _send_offer(sender: _ButtonSender) -> str:
    return asyncio.run(
        webhook_module._send_text_or_offer(
            sender,
            conversation_id=1,
            to_phone="966500000001",
            text="Shall I pass this to a colleague to confirm your booking?",
            offer=booking_offer_buttons(7, "en"),
        )
    )


def test_an_offer_goes_out_with_its_buttons() -> None:
    sender = _ButtonSender()

    assert _send_offer(sender) == "wamid.BUTTONS"
    assert [kind for kind, _ in sender.sends] == ["buttons"]


def test_a_definitely_rejected_button_send_is_sent_again_as_plain_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Owner decisions 2026-10-01: refused as invalid, nothing was sent, so
    the same body goes once more as plain text -- the offer question is in
    it, so a typed yes still works."""
    sender = _ButtonSender(
        buttons_error=WhatsAppMessageRejectedError(
            "WhatsApp send failed: HTTPStatusError (status=400, code=131009)"
        )
    )
    caplog.set_level(logging.ERROR, logger="services.agent.webhook")

    assert _send_offer(sender) == "wamid.TEXT"
    assert [kind for kind, _ in sender.sends] == ["buttons", "text"]
    assert sender.sends[0][1] == sender.sends[1][1]
    (failure,) = [json.loads(record.getMessage()) for record in caplog.records]
    assert failure["event"] == "booking_offer_buttons_rejected"
    assert failure["quote_id"] == 7


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            WhatsAppSendError("WhatsApp send failed: ReadTimeout"), id="timeout"
        ),
        pytest.param(
            WhatsAppSendError(
                "WhatsApp send failed: HTTPStatusError (status=400, code=130429)"
            ),
            id="rate-limit",
        ),
        pytest.param(RuntimeError("unexpected"), id="anything-else"),
    ],
)
def test_an_ambiguous_button_failure_is_never_retried(error: Exception) -> None:
    """The offer may already have reached the customer: never send it a
    second time. The failure goes up to the funnel."""
    sender = _ButtonSender(buttons_error=error)

    with pytest.raises(type(error)):
        _send_offer(sender)

    assert [kind for kind, _ in sender.sends] == ["buttons"]


def test_text_without_an_offer_is_sent_as_plain_text() -> None:
    sender = _ButtonSender()

    message_id = asyncio.run(
        webhook_module._send_text_or_offer(
            sender, conversation_id=1, to_phone="966500000001", text="hi", offer=None
        )
    )

    assert message_id == "wamid.TEXT"
    assert sender.sends == [("text", "hi")]


def test_load_webhook_settings_with_a_valid_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "sign-me")

    settings = load_webhook_settings()

    assert settings.verify_token == "verify-me"
    assert settings.app_secret == "sign-me"


def test_load_webhook_settings_requires_verify_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WHATSAPP_VERIFY_TOKEN", raising=False)
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "sign-me")

    with pytest.raises(WebhookConfigurationError, match="WHATSAPP_VERIFY_TOKEN"):
        load_webhook_settings()


def test_load_webhook_settings_requires_app_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)

    with pytest.raises(WebhookConfigurationError, match="WHATSAPP_APP_SECRET"):
        load_webhook_settings()


def test_get_webhook_settings_delegates_to_load_webhook_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-me")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "sign-me")

    settings = get_webhook_settings()

    assert settings.verify_token == "verify-me"
    assert settings.app_secret == "sign-me"


_TEST_LLM_ENV = {
    "LLM_MODEL": "test-model-v1",
    "LLM_API_KEY": "test-key",
    "MAX_CONVERSATION_TURNS": "20",
    "LLM_MAX_TOKENS_PER_CONVERSATION": "50000",
    "LLM_MAX_SPEND_PER_DAY_USD": "5.00",
    "MAX_MESSAGES_PER_NUMBER_PER_DAY": "50",
    "LLM_MAX_TOKENS_PER_NUMBER_PER_DAY": "100000",
}


def test_get_llm_settings_delegates_to_load_llm_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "services.agent.llm.config.ALLOWED_MODELS", frozenset({"test-model-v1"})
    )
    for key, value in _TEST_LLM_ENV.items():
        monkeypatch.setenv(key, value)

    settings = get_llm_settings()

    assert settings.model == "test-model-v1"
    assert settings.max_tokens_per_conversation == 50_000


def test_get_model_transport_returns_a_real_gemini_transport() -> None:
    settings = LlmSettings(
        model="test-model-v1",
        api_key="test-key",
        timeout_ms=20_000,
        max_conversation_turns=20,
        max_tokens_per_conversation=50_000,
        max_spend_per_day_usd=Decimal("5.00"),
        max_messages_per_number_per_day=50,
        max_tokens_per_number_per_day=100_000,
    )

    transport = get_model_transport(settings)

    assert isinstance(transport, GeminiTransport)


def _openrouter_settings(providers: tuple[str, ...]) -> LlmSettings:
    return LlmSettings(
        model="vendor/model-1",
        api_key="test-openrouter-key",
        timeout_ms=10_000,
        max_conversation_turns=20,
        max_tokens_per_conversation=50_000,
        max_spend_per_day_usd=Decimal("5.00"),
        max_messages_per_number_per_day=50,
        max_tokens_per_number_per_day=100_000,
        openrouter_route=OpenRouterRoute(
            providers=providers,
            token_rates=TokenRates(
                input_usd_per_million_tokens=Decimal("0.50"),
                output_usd_per_million_tokens=Decimal("2.00"),
            ),
        ),
    )


def test_get_model_transport_returns_an_openrouter_transport_for_a_routed_model() -> (
    None
):
    transport = get_model_transport(_openrouter_settings(("provider-a",)))

    assert isinstance(transport, OpenRouterTransport)


def test_get_model_transport_refuses_a_route_with_no_approved_provider() -> None:
    with pytest.raises(LlmConfigurationError, match="no OpenRouter provider"):
        get_model_transport(_openrouter_settings(()))


def test_get_model_transport_builds_a_transport_for_the_shipped_glm_route() -> None:
    settings = load_llm_settings(
        {
            "LLM_MODEL": "z-ai/glm-5.3-20260816",
            "OPENROUTER_API_KEY": "test-openrouter-key",
            "MAX_CONVERSATION_TURNS": "20",
            "LLM_MAX_TOKENS_PER_CONVERSATION": "50000",
            "LLM_MAX_SPEND_PER_DAY_USD": "5.00",
            "MAX_MESSAGES_PER_NUMBER_PER_DAY": "50",
            "LLM_MAX_TOKENS_PER_NUMBER_PER_DAY": "100000",
        }
    )

    transport = get_model_transport(settings)

    assert isinstance(transport, OpenRouterTransport)
    assert transport._reasoning_effort == "low"


# --- the no-silence funnel (CLAUDE.md rule 12) --------------------------------


@pytest.mark.parametrize(
    ("escalated", "delivered", "status"),
    [
        (True, True, "escalated"),
        (True, False, "escalated_undelivered"),
        (False, True, "notified_no_escalation"),
        (False, False, "failed_unrecorded"),
    ],
)
def test_funnel_status_names_every_combination_honestly(
    escalated: bool, delivered: bool, status: str
) -> None:
    assert _funnel_status(escalated=escalated, delivered=delivered) == status


@dataclass
class _FakeConnection:
    """Stands in for a psycopg connection: the funnel only ever asks it
    whether it is closed. Everything that would run SQL on it is faked by
    _FunnelFakes below."""

    closed: bool = False


@dataclass
class _FunnelFakes:
    """Replaces every database and WhatsApp call the funnel makes, and
    records which connection each ran on. A call on a connection marked
    closed fails the way psycopg does."""

    fresh: _FakeConnection = field(default_factory=_FakeConnection)
    close_after_insert: bool = False
    escalations_on: list[_FakeConnection] = field(default_factory=list)
    guard_checks_on: list[_FakeConnection] = field(default_factory=list)
    sends: list[tuple[str, str]] = field(default_factory=list)
    language_lookups_on: list[_FakeConnection] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _open_escalation(conn: _FakeConnection, **_kwargs: Any) -> int:
            self.escalations_on.append(conn)
            if conn.closed:
                raise psycopg.OperationalError("the connection is closed")
            if self.close_after_insert:
                conn.closed = True
            return 42

        def _customer_language(conn: _FakeConnection, _conversation_id: int) -> str:
            self.language_lookups_on.append(conn)
            if conn.closed:
                raise psycopg.OperationalError("the connection is closed")
            return "en"

        def _enforce(conn: _FakeConnection, **_kwargs: Any) -> GuardVerdict:
            self.guard_checks_on.append(conn)
            if conn.closed:
                raise psycopg.OperationalError("the connection is closed")
            return GuardVerdict(
                allowed=True, findings=(), quote_ids=(), escalation_id=None
            )

        fakes = self

        class _Sender:
            async def send_text(self, *, to_phone: str, body: str) -> str:
                fakes.sends.append((to_phone, body))
                return "wamid.OUTBOUND-UNIT"

        @contextlib.contextmanager
        def _fresh_connection() -> Iterator[_FakeConnection]:
            yield self.fresh

        monkeypatch.setattr(webhook_module, "open_escalation", _open_escalation)
        monkeypatch.setattr(webhook_module, "enforce_outbound_text", _enforce)
        monkeypatch.setattr(webhook_module, "customer_language", _customer_language)
        monkeypatch.setattr(webhook_module, "get_whatsapp_send_settings", lambda: None)
        monkeypatch.setattr(webhook_module, "get_whatsapp_sender", lambda _s: _Sender())
        monkeypatch.setattr(
            webhook_module, "_insert_outbound_message", lambda *_a, **_k: None
        )
        monkeypatch.setattr(webhook_module, "get_db_connection", _fresh_connection)


def _run_funnel(conn: _FakeConnection) -> str:
    return asyncio.run(
        webhook_module._escalate_and_notify(
            cast(Any, conn),
            conversation_id=1,
            customer_phone="+966500000001",
            reason="internal_error",
            exc=RuntimeError("anything"),
        )
    )


def test_funnel_retries_both_halves_on_a_fresh_connection_when_its_own_died(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The turn's connection is already dead (the database itself is up):
    the escalation and the fallback both fail on it, and both are retried,
    once, on a fresh connection -- one lost connection is not a silent
    turn."""
    fakes = _FunnelFakes()
    fakes.install(monkeypatch)
    dead = _FakeConnection(closed=True)

    status = _run_funnel(dead)

    assert status == "escalated"
    assert fakes.escalations_on == [dead, fakes.fresh]
    assert fakes.guard_checks_on == [dead, fakes.fresh]
    # The language is read again on the fresh connection: the retried
    # fallback goes out in the customer's language, not bilingual.
    assert fakes.language_lookups_on == [dead, fakes.fresh]
    assert fakes.sends == [("966500000001", FALLBACK.english)]


def test_funnel_retries_only_the_failed_half_so_nothing_is_opened_or_sent_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The escalation was opened, then the connection died before the
    fallback: only the fallback is retried -- a second escalation for the
    same turn would be noise for the human handling it."""
    fakes = _FunnelFakes(close_after_insert=True)
    fakes.install(monkeypatch)
    conn = _FakeConnection()

    status = _run_funnel(conn)

    assert status == "escalated"
    assert fakes.escalations_on == [conn]
    assert fakes.guard_checks_on == [conn, fakes.fresh]
    assert len(fakes.sends) == 1


def test_funnel_does_not_reconnect_when_its_connection_is_fine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure that is not a lost connection (here the send itself) is
    reported honestly and not retried on a new connection."""
    fakes = _FunnelFakes()
    fakes.install(monkeypatch)

    async def _refused(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(webhook_module, "_send_or_log_failure", _refused)

    def _no_reconnect() -> Any:
        raise AssertionError("the funnel must not reconnect for this failure")

    monkeypatch.setattr(webhook_module, "get_db_connection", _no_reconnect)

    assert _run_funnel(_FakeConnection()) == "escalated_undelivered"


def test_funnel_reports_failed_unrecorded_when_no_fresh_connection_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The database is really down: the retry cannot open a connection
    either. Logged, never raised -- the documented residual."""
    fakes = _FunnelFakes()
    fakes.install(monkeypatch)

    def _database_down() -> Any:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(webhook_module, "get_db_connection", _database_down)

    assert _run_funnel(_FakeConnection(closed=True)) == "failed_unrecorded"
    assert fakes.sends == []
