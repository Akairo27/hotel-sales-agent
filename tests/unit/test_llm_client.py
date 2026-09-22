"""GeminiTransport's own responsibility: build the right call, retry a
transient failure up to _RETRY_ATTEMPTS times with logged backoff, and turn
a final failure into ModelUnavailableError (CLAUDE.md §8 — every external
call has explicit failure handling). No real network access or API key is
used here — the SDK client's own generate_content method is replaced with
a stub that raises or returns, the same shape a real timeout, API error, or
success would produce.

No pytest-asyncio dependency: asyncio.run drives the coroutine directly,
the same pattern tests/unit/test_agent_main.py already uses for the one
other async function in this repository.
"""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from typing import Any

import httpx
import pytest
from google.genai import errors, types

from services.agent.llm import client as client_module
from services.agent.llm.client import GeminiTransport, _retry_delay_seconds
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


def _text_response(text: str) -> types.GenerateContentResponse:
    content = types.Content(role="model", parts=[types.Part.from_text(text=text)])
    return types.GenerateContentResponse(candidates=[types.Candidate(content=content)])


def _disable_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test below that exercises a retryable failure now drives a
    real retry loop (client.py owns it, rather than the SDK hiding it
    inside the method these tests replace) -- without this, a test using
    a retryable error code would actually sleep for real between
    attempts. Patches asyncio.sleep in client.py's own namespace only, so
    the test still proves the real number of attempts happened."""

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("services.agent.llm.client.asyncio.sleep", _no_sleep)


def _patch_sdk_call_to_raise(
    monkeypatch: pytest.MonkeyPatch, transport: GeminiTransport, exc: Exception
) -> None:
    """Every call raises the same exception -- for a retryable exc this
    drives GeminiTransport.generate through all _RETRY_ATTEMPTS before it
    gives up, so backoff is disabled here too."""
    _disable_retry_backoff(monkeypatch)

    async def _raise(*_args: Any, **_kwargs: Any) -> types.GenerateContentResponse:
        raise exc

    monkeypatch.setattr(transport._client.aio.models, "generate_content", _raise)


def _patch_sdk_call_with_sequence(
    monkeypatch: pytest.MonkeyPatch,
    transport: GeminiTransport,
    outcomes: list[Exception | types.GenerateContentResponse],
) -> list[int]:
    """Returns/raises each entry in outcomes in order, one per call.
    Returns the list this helper appends to on every call, so a test can
    assert exactly how many calls happened."""
    _disable_retry_backoff(monkeypatch)
    calls: list[int] = []
    remaining = list(outcomes)

    async def _next(*_args: Any, **_kwargs: Any) -> types.GenerateContentResponse:
        calls.append(len(calls) + 1)
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(transport._client.aio.models, "generate_content", _next)
    return calls


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


def test_init_does_not_configure_the_sdks_own_retry() -> None:
    """The inverse of what this module used to assert: retries are now
    owned by generate() itself (see client.py's top comment for why --
    the SDK gives no hook to log an individual attempt safely), so
    HttpOptions.retry_options must stay unset. If it were set, the SDK
    would retry internally AND generate() would retry again on top of
    that -- exactly the double-retry stacking client.py's comments have
    always warned against, just from the opposite direction now."""
    transport = GeminiTransport(_SETTINGS)
    retry_options = transport._client._api_client._http_options.retry_options
    assert retry_options is None


def test_generate_retries_a_transient_api_error_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = GeminiTransport(_SETTINGS)
    calls = _patch_sdk_call_with_sequence(
        monkeypatch,
        transport,
        [
            errors.ServerError(code=503, response_json={"error": {}}),
            errors.ServerError(code=503, response_json={"error": {}}),
            _text_response("back online"),
        ],
    )

    response = asyncio.run(
        transport.generate(contents=[], system_instruction="be helpful")
    )

    assert response.text == "back online"
    assert calls == [1, 2, 3]


def test_generate_does_not_retry_a_non_retryable_api_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """400 API_KEY_INVALID is a caller-caused, permanent failure -- no
    number of retries fixes a bad key, so it must fail on the first
    attempt, not burn the full backoff budget."""
    transport = GeminiTransport(_SETTINGS)
    calls = _patch_sdk_call_with_sequence(
        monkeypatch,
        transport,
        [errors.ClientError(code=400, response_json={"error": {}})],
    )

    with pytest.raises(ModelUnavailableError):
        asyncio.run(transport.generate(contents=[], system_instruction="be helpful"))

    assert calls == [1]


def test_generate_exhausts_retries_and_raises_after_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = GeminiTransport(_SETTINGS)
    calls = _patch_sdk_call_with_sequence(
        monkeypatch,
        transport,
        [
            errors.ServerError(code=503, response_json={"error": {}}),
            errors.ServerError(code=503, response_json={"error": {}}),
            errors.ServerError(code=503, response_json={"error": {}}),
        ],
    )

    with pytest.raises(ModelUnavailableError):
        asyncio.run(transport.generate(contents=[], system_instruction="be helpful"))

    assert calls == [1, 2, 3]


def test_generate_logs_a_warning_for_each_retried_attempt(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Attempt number, exception type, and elapsed time only -- and
    specifically proves the leaked-response-body pattern
    test_generate_api_error_message_never_contains_the_raw_response_body
    guards on the final exception doesn't reappear here, on the
    per-attempt retry log instead."""
    transport = GeminiTransport(_SETTINGS)
    secret_detail = "do-not-leak-this-response-detail"
    calls = _patch_sdk_call_with_sequence(
        monkeypatch,
        transport,
        [
            errors.ServerError(
                code=503, response_json={"error": {"message": secret_detail}}
            ),
            errors.ServerError(
                code=503, response_json={"error": {"message": secret_detail}}
            ),
            _text_response("recovered"),
        ],
    )
    caplog.set_level(logging.WARNING, logger="services.agent.llm.client")

    asyncio.run(transport.generate(contents=[], system_instruction="be helpful"))

    assert calls == [1, 2, 3]
    retry_records = [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.levelno == logging.WARNING
    ]
    assert len(retry_records) == 2
    for expected_attempt, record in enumerate(retry_records, start=1):
        assert record["event"] == "model_call_retry"
        assert record["attempt"] == expected_attempt
        assert record["max_attempts"] == 3
        assert record["exception_type"] == "ServerError"
        assert isinstance(record["elapsed_ms"], int)
        assert record["elapsed_ms"] >= 0
        assert secret_detail not in json.dumps(record)


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


def test_retry_delay_seconds_matches_the_pinned_backoff_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same formula as tenacity.wait_exponential_jitter (verified against
    the installed tenacity==9.1.4's source in the PR that added this):
    min(initial * exp_base ** (attempt - 1) + uniform(0, jitter),
    max_delay). The jitter source's uniform() is pinned so the
    exponential and jitter terms are each independently checkable."""
    monkeypatch.setattr(client_module._jitter_random, "uniform", lambda _a, _b: 0.0)
    assert _retry_delay_seconds(1) == 1.0  # 1.0 * 2**0 + 0
    assert _retry_delay_seconds(2) == 2.0  # 1.0 * 2**1 + 0
    assert _retry_delay_seconds(3) == 4.0  # 1.0 * 2**2 + 0

    monkeypatch.setattr(client_module._jitter_random, "uniform", lambda _a, _b: 1.0)
    assert _retry_delay_seconds(1) == 2.0  # 1.0 * 2**0 + 1.0
    # attempt 4 is past _RETRY_ATTEMPTS in real use, but the formula must
    # still clamp correctly: 1.0 * 2**3 + 1.0 = 9.0, clipped to max_delay.
    assert _retry_delay_seconds(4) == 5.0
