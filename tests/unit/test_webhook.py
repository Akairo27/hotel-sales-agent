"""Unit tests for services/agent/webhook.py's pure helpers — no database,
no network. _signature_is_valid, _parse_inbound_message, and
load_webhook_settings each have their own dedicated tests here rather than
being exercised only incidentally through the integration tests, matching
this repo's usual layering (e.g. output_guard/extraction.py vs.
enforcement.py).
"""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal

import pytest

from services.agent.llm.client import GeminiTransport
from services.agent.llm.config import LlmSettings
from services.agent.webhook import (
    WebhookConfigurationError,
    _normalize_phone,
    _parse_inbound_message,
    _signature_is_valid,
    get_llm_settings,
    get_model_transport,
    get_webhook_settings,
    load_webhook_settings,
)

_SECRET = "shared-secret"


def _signature_for(body: bytes, secret: str = _SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_signature_is_valid_accepts_a_correctly_signed_body() -> None:
    body = b'{"hello": "world"}'
    assert _signature_is_valid(
        body=body, signature_header=_signature_for(body), app_secret=_SECRET
    )


def test_signature_is_valid_rejects_a_signature_from_a_different_secret() -> None:
    body = b'{"hello": "world"}'
    wrong_signature = _signature_for(body, secret="a-different-secret")
    assert not _signature_is_valid(
        body=body, signature_header=wrong_signature, app_secret=_SECRET
    )


def test_signature_is_valid_rejects_a_signature_for_a_different_body() -> None:
    signed_for_other_body = _signature_for(b'{"other": "body"}')
    assert not _signature_is_valid(
        body=b'{"hello": "world"}',
        signature_header=signed_for_other_body,
        app_secret=_SECRET,
    )


def test_signature_is_valid_rejects_a_missing_header() -> None:
    assert not _signature_is_valid(
        body=b"anything", signature_header=None, app_secret=_SECRET
    )


def test_signature_is_valid_rejects_a_header_without_the_sha256_prefix() -> None:
    body = b'{"hello": "world"}'
    bare_digest = hmac.new(_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    assert not _signature_is_valid(
        body=body, signature_header=bare_digest, app_secret=_SECRET
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


def test_parse_inbound_message_extracts_phone_name_id_and_body() -> None:
    inbound = _parse_inbound_message(_payload())

    assert inbound is not None
    assert inbound.customer_phone == "+966500000001"
    assert inbound.customer_name == "Test Customer"
    assert inbound.whatsapp_message_id == "wamid.1"
    assert inbound.body == "hello"


def test_parse_inbound_message_returns_none_when_there_are_no_messages() -> None:
    assert _parse_inbound_message(_payload(messages=[])) is None


def test_parse_inbound_message_returns_none_for_a_status_callback_payload() -> None:
    """A delivery/read status callback has no "messages" key at all —
    Meta sends these to the same URL as inbound messages."""
    payload = {
        "entry": [
            {"changes": [{"value": {"messaging_product": "whatsapp", "statuses": []}}]}
        ]
    }
    assert _parse_inbound_message(payload) is None


def test_parse_inbound_message_returns_none_for_a_non_text_message() -> None:
    payload = _payload(
        messages=[{"from": "966500000001", "id": "wamid.2", "type": "image"}]
    )
    assert _parse_inbound_message(payload) is None


def test_parse_inbound_message_falls_back_to_from_field_without_contacts() -> None:
    payload = _payload(contacts=[])
    inbound = _parse_inbound_message(payload)

    assert inbound is not None
    assert inbound.customer_phone == "+966500000001"
    assert inbound.customer_name is None


def test_parse_inbound_message_returns_none_for_a_malformed_payload() -> None:
    assert _parse_inbound_message({"entry": []}) is None
    assert _parse_inbound_message({}) is None


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
    )

    transport = get_model_transport(settings)

    assert isinstance(transport, GeminiTransport)
