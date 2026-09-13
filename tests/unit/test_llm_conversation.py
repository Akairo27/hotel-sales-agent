"""Unit tests for the tool-calling loop itself, with no database and no
network: load_conversation_state, load_recent_messages, and dispatch_tool
are monkeypatched to fakes, and the model transport is a fake driven by a
canned list of responses. What this file actually exercises is
conversation.py's own orchestration — the turn cap, the tool-call ->
function-response round trip, usage accumulation, and the loop-iteration
limit — not context.py or dispatch.py, which have their own tests.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from google.genai import types

from services.agent.llm import conversation as conversation_module
from services.agent.llm.config import MAX_TOOL_ITERATIONS, LlmSettings
from services.agent.llm.context import ConversationState
from services.agent.llm.conversation import generate_reply
from services.agent.llm.errors import (
    DailySpendCapExceededError,
    TokenSpendCapExceededError,
    ToolLoopLimitError,
    TurnCapExceededError,
    UsageUnavailableError,
)

_NOW = datetime(2026, 9, 1, tzinfo=UTC)
_NOT_A_CONNECTION: Any = object()

_SETTINGS = LlmSettings(
    model="test-model-v1",
    api_key="test-key",
    timeout_ms=20_000,
    max_conversation_turns=20,
    max_tokens_per_conversation=50_000,
    max_spend_per_day_usd=Decimal("5.00"),
    max_messages_per_number_per_day=50,
)


@dataclass
class FakeTransport:
    responses: list[types.GenerateContentResponse]
    calls: list[list[types.Content]] = field(default_factory=list)

    async def generate(
        self, *, contents: list[types.Content], system_instruction: str
    ) -> types.GenerateContentResponse:
        del system_instruction  # unused: this fake only records `contents`
        self.calls.append(list(contents))
        return self.responses.pop(0)


def _text_response(
    text: str, *, total_tokens: int = 10
) -> types.GenerateContentResponse:
    content = types.Content(role="model", parts=[types.Part.from_text(text=text)])
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=content)],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=total_tokens - 2,
            candidates_token_count=2,
            total_token_count=total_tokens,
        ),
    )


def _function_call_response(
    name: str, args: dict[str, Any], *, total_tokens: int = 12
) -> types.GenerateContentResponse:
    content = types.Content(
        role="model", parts=[types.Part.from_function_call(name=name, args=args)]
    )
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=content)],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=total_tokens - 2,
            candidates_token_count=2,
            total_token_count=total_tokens,
        ),
    )


def _stub_conversation_state(
    monkeypatch: pytest.MonkeyPatch, *, turn_count: int
) -> None:
    monkeypatch.setattr(
        conversation_module,
        "load_conversation_state",
        lambda _conn, conversation_id: ConversationState(
            id=conversation_id,
            customer_phone="+966500000001",
            active_quote_id=None,
            concession_count=0,
            turn_count=turn_count,
        ),
    )
    monkeypatch.setattr(
        conversation_module,
        "load_recent_messages",
        lambda _conn, _conversation_id, **_kwargs: [],
    )
    monkeypatch.setattr(
        conversation_module,
        "check_token_spend_caps",
        lambda _conn, **_kwargs: None,
    )


def test_generate_reply_returns_text_when_model_calls_no_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_conversation_state(monkeypatch, turn_count=0)
    transport = FakeTransport([_text_response("hello", total_tokens=28)])

    reply = asyncio.run(
        generate_reply(
            _NOT_A_CONNECTION,
            conversation_id=1,
            customer_name=None,
            transport=transport,
            settings=_SETTINGS,
            now=_NOW,
        )
    )

    assert reply.text == "hello"
    assert reply.tool_calls == ()
    assert reply.quote_ids == ()
    assert reply.usage.total_tokens == 28
    assert len(transport.calls) == 1


def test_generate_reply_executes_a_tool_call_then_returns_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_conversation_state(monkeypatch, turn_count=0)
    fake_result = {"priced": True, "quote_id": 99, "total_price_display": "150.00 SAR"}
    recorded_calls: list[tuple[str, dict[str, Any]]] = []

    def _fake_dispatch(
        _conn: Any, name: str, args: dict[str, Any], **_kwargs: Any
    ) -> dict[str, Any]:
        recorded_calls.append((name, args))
        return fake_result

    monkeypatch.setattr(conversation_module, "dispatch_tool", _fake_dispatch)

    call_args = {"hotel_id": 1, "room_type_id": 2, "check_in": "2026-09-01"}
    transport = FakeTransport(
        [
            _function_call_response("get_quote", call_args),
            _text_response("here is your price"),
        ]
    )

    reply = asyncio.run(
        generate_reply(
            _NOT_A_CONNECTION,
            conversation_id=1,
            customer_name=None,
            transport=transport,
            settings=_SETTINGS,
            now=_NOW,
        )
    )

    assert reply.text == "here is your price"
    assert recorded_calls == [("get_quote", call_args)]
    assert [call.name for call in reply.tool_calls] == ["get_quote"]
    assert reply.tool_calls[0].result == fake_result
    assert reply.quote_ids == (99,)
    # The model's function-call turn and our function-response turn were
    # both appended before the second model call.
    assert len(transport.calls) == 2
    assert len(transport.calls[1]) == 2
    assert transport.calls[1][1].parts is not None
    assert transport.calls[1][1].parts[0].function_response is not None


def test_generate_reply_refuses_at_the_turn_cap_without_calling_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_conversation_state(monkeypatch, turn_count=_SETTINGS.max_conversation_turns)
    transport = FakeTransport([_text_response("should never be reached")])

    with pytest.raises(TurnCapExceededError):
        asyncio.run(
            generate_reply(
                _NOT_A_CONNECTION,
                conversation_id=1,
                customer_name=None,
                transport=transport,
                settings=_SETTINGS,
                now=_NOW,
            )
        )
    assert transport.calls == []


def test_generate_reply_raises_after_exceeding_the_tool_iteration_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_conversation_state(monkeypatch, turn_count=0)
    monkeypatch.setattr(
        conversation_module,
        "dispatch_tool",
        lambda _conn, _name, _args, **_kwargs: {
            "priced": False,
            "reason": "loops_forever",
        },
    )
    responses = [
        _function_call_response("check_availability", {"hotel_id": 1})
        for _ in range(MAX_TOOL_ITERATIONS)
    ]
    transport = FakeTransport(responses)

    with pytest.raises(ToolLoopLimitError):
        asyncio.run(
            generate_reply(
                _NOT_A_CONNECTION,
                conversation_id=1,
                customer_name=None,
                transport=transport,
                settings=_SETTINGS,
                now=_NOW,
            )
        )
    assert len(transport.calls) == MAX_TOOL_ITERATIONS


def test_generate_reply_refuses_when_the_conversation_token_cap_is_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_conversation_state(monkeypatch, turn_count=0)

    def _raise_token_cap(
        _conn: Any, *, conversation_id: int, now: Any, settings: Any
    ) -> None:
        del conversation_id, now, settings
        raise TokenSpendCapExceededError("conversation 1 is at its token cap")

    monkeypatch.setattr(conversation_module, "check_token_spend_caps", _raise_token_cap)
    transport = FakeTransport([_text_response("should never be reached")])

    with pytest.raises(TokenSpendCapExceededError):
        asyncio.run(
            generate_reply(
                _NOT_A_CONNECTION,
                conversation_id=1,
                customer_name=None,
                transport=transport,
                settings=_SETTINGS,
                now=_NOW,
            )
        )
    assert transport.calls == []


def test_generate_reply_refuses_when_the_daily_spend_cap_is_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_conversation_state(monkeypatch, turn_count=0)

    def _raise_daily_cap(
        _conn: Any, *, conversation_id: int, now: Any, settings: Any
    ) -> None:
        del conversation_id, now, settings
        raise DailySpendCapExceededError("today's spend is at the daily cap")

    monkeypatch.setattr(conversation_module, "check_token_spend_caps", _raise_daily_cap)
    transport = FakeTransport([_text_response("should never be reached")])

    with pytest.raises(DailySpendCapExceededError):
        asyncio.run(
            generate_reply(
                _NOT_A_CONNECTION,
                conversation_id=1,
                customer_name=None,
                transport=transport,
                settings=_SETTINGS,
                now=_NOW,
            )
        )
    assert transport.calls == []


def test_generate_reply_raises_usage_unavailable_when_metadata_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_conversation_state(monkeypatch, turn_count=0)
    content = types.Content(role="model", parts=[types.Part.from_text(text="hi")])
    response_with_no_usage = types.GenerateContentResponse(
        candidates=[types.Candidate(content=content)], usage_metadata=None
    )
    transport = FakeTransport([response_with_no_usage])

    with pytest.raises(UsageUnavailableError):
        asyncio.run(
            generate_reply(
                _NOT_A_CONNECTION,
                conversation_id=1,
                customer_name=None,
                transport=transport,
                settings=_SETTINGS,
                now=_NOW,
            )
        )


def test_generate_reply_folds_thinking_tokens_into_candidates_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini bills extended-thinking tokens at the output rate but the
    SDK reports them in their own thoughts_token_count field, separate
    from candidates_token_count — _usage_from must fold them in so
    pricing.estimate_cost_usd's two-bucket formula doesn't silently
    undercount a call that used extended thinking."""
    _stub_conversation_state(monkeypatch, turn_count=0)
    content = types.Content(role="model", parts=[types.Part.from_text(text="hi")])
    response_with_thinking = types.GenerateContentResponse(
        candidates=[types.Candidate(content=content)],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=10,
            candidates_token_count=5,
            thoughts_token_count=40,
            total_token_count=55,
        ),
    )
    transport = FakeTransport([response_with_thinking])

    reply = asyncio.run(
        generate_reply(
            _NOT_A_CONNECTION,
            conversation_id=1,
            customer_name=None,
            transport=transport,
            settings=_SETTINGS,
            now=_NOW,
        )
    )

    assert reply.usage.prompt_tokens == 10
    assert reply.usage.candidates_tokens == 45
    assert reply.usage.total_tokens == 55


def test_generate_reply_treats_absent_thinking_tokens_as_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """thoughts_token_count is legitimately None for a call that used no
    extended thinking — unlike the three required usage fields, its
    absence must not raise UsageUnavailableError."""
    _stub_conversation_state(monkeypatch, turn_count=0)
    transport = FakeTransport([_text_response("hello", total_tokens=28)])

    reply = asyncio.run(
        generate_reply(
            _NOT_A_CONNECTION,
            conversation_id=1,
            customer_name=None,
            transport=transport,
            settings=_SETTINGS,
            now=_NOW,
        )
    )

    assert reply.usage.candidates_tokens == 2


def test_generate_reply_raises_usage_unavailable_when_metadata_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_conversation_state(monkeypatch, turn_count=0)
    content = types.Content(role="model", parts=[types.Part.from_text(text="hi")])
    response_with_partial_usage = types.GenerateContentResponse(
        candidates=[types.Candidate(content=content)],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=10,
            candidates_token_count=None,
            total_token_count=None,
        ),
    )
    transport = FakeTransport([response_with_partial_usage])

    with pytest.raises(UsageUnavailableError):
        asyncio.run(
            generate_reply(
                _NOT_A_CONNECTION,
                conversation_id=1,
                customer_name=None,
                transport=transport,
                settings=_SETTINGS,
                now=_NOW,
            )
        )
