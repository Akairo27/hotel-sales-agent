"""GeminiTransport's own responsibility is narrow: build the right call and
turn an SDK-level failure into ModelUnavailableError (CLAUDE.md §8 — every
external call has explicit failure handling). No real network access or
API key is used here — the SDK client's own generate_content method is
replaced with a stub that raises, the same failure shape a real timeout
or API error would produce.

No pytest-asyncio dependency: asyncio.run drives the coroutine directly,
the same pattern tests/unit/test_agent_main.py already uses for the one
other async function in this repository.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
import pytest
from google.genai import errors, types

from services.agent.llm.client import GeminiTransport
from services.agent.llm.config import LlmSettings
from services.agent.llm.errors import ModelUnavailableError

_SETTINGS = LlmSettings(
    model="test-model-v1",
    api_key="test-key",
    timeout_ms=20_000,
    max_conversation_turns=20,
    max_tokens_per_conversation=50_000,
    max_spend_per_day_usd=Decimal("5.00"),
    max_messages_per_number_per_day=50,
)


def _patch_sdk_call_to_raise(
    monkeypatch: pytest.MonkeyPatch, transport: GeminiTransport, exc: Exception
) -> None:
    async def _raise(*_args: Any, **_kwargs: Any) -> types.GenerateContentResponse:
        raise exc

    monkeypatch.setattr(transport._client.aio.models, "generate_content", _raise)


def test_generate_wraps_api_error_as_model_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = GeminiTransport(_SETTINGS)
    _patch_sdk_call_to_raise(
        monkeypatch,
        transport,
        errors.ClientError(
            code=429, response_json={"error": {"message": "rate limited"}}
        ),
    )
    with pytest.raises(ModelUnavailableError):
        asyncio.run(transport.generate(contents=[], system_instruction="be helpful"))


def test_generate_wraps_transport_timeout_as_model_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = GeminiTransport(_SETTINGS)
    _patch_sdk_call_to_raise(monkeypatch, transport, httpx.ReadTimeout("timed out"))
    with pytest.raises(ModelUnavailableError):
        asyncio.run(transport.generate(contents=[], system_instruction="be helpful"))


def test_generate_configures_the_sdks_own_retry_with_the_pinned_values() -> None:
    """google-genai wraps every call in tenacity, but only retries when
    HttpOptions.retry_options is set -- left unset (as this module did
    before), it resolves to exactly one attempt, no retry at all. This
    asserts the values this module pins actually reach the underlying
    API client, rather than stacking a second, hand-rolled retry layer
    on top of the SDK's own (see client.py's own module comment for the
    incident and reasoning that led to these exact numbers)."""
    transport = GeminiTransport(_SETTINGS)
    retry_options = transport._client._api_client._http_options.retry_options

    assert retry_options is not None
    assert retry_options.attempts == 3
    assert retry_options.initial_delay == 1.0
    assert retry_options.max_delay == 5.0
    assert retry_options.exp_base == 2.0
    assert retry_options.jitter == 1.0
    assert retry_options.http_status_codes == [408, 429, 500, 502, 503, 504]


def test_generate_api_error_message_never_contains_the_raw_response_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same leak pattern whatsapp_send.py's WhatsAppSendError fix closed
    for the Graph API, never reached here until now: APIError's own
    __str__ includes exc.details, Google's full raw response body,
    verbatim -- and this call carries a live API key
    (services/agent/llm/config.py's LLM_API_KEY, sent as the
    x-goog-api-key header). Only .code and .status, Google's own short
    status string, are safe to surface."""
    transport = GeminiTransport(_SETTINGS)
    secret_detail = "do-not-leak-this-response-detail"
    _patch_sdk_call_to_raise(
        monkeypatch,
        transport,
        errors.ServerError(
            code=503,
            response_json={
                "error": {"status": "UNAVAILABLE", "message": secret_detail}
            },
        ),
    )

    with pytest.raises(ModelUnavailableError) as exc_info:
        asyncio.run(transport.generate(contents=[], system_instruction="be helpful"))

    message = str(exc_info.value)
    assert secret_detail not in message
    assert "503" in message
    assert "UNAVAILABLE" in message


def test_generate_transport_error_message_never_contains_the_raw_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same property for a transport-level (non-API) failure: httpx
    exceptions don't put headers in their own message today (verified
    directly in whatsapp_send.py's equivalent fix), but this module
    doesn't rely on that holding forever either -- only the exception's
    type name is surfaced."""
    transport = GeminiTransport(_SETTINGS)
    secret_detail = "do-not-leak-this-either"
    _patch_sdk_call_to_raise(monkeypatch, transport, httpx.ConnectError(secret_detail))

    with pytest.raises(ModelUnavailableError) as exc_info:
        asyncio.run(transport.generate(contents=[], system_instruction="be helpful"))

    message = str(exc_info.value)
    assert secret_detail not in message
    assert "ConnectError" in message
