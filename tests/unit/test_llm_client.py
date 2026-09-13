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
