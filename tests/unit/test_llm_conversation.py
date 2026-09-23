"""Unit tests for the tool-calling loop itself, with no database and no
network: load_conversation_state, load_recent_messages, and dispatch_tool
are monkeypatched to fakes, and the model transport is a fake driven by a
canned list of provider-neutral responses (services.agent.llm.model_types).
What this file actually exercises is conversation.py's own orchestration —
the turn cap, the tool-call -> tool-result round trip, usage accumulation,
and the loop-iteration limit — not context.py or dispatch.py, which have
their own tests, and not a specific provider's response parsing, which
belongs to that provider's own transport tests (e.g.
tests/unit/test_llm_client.py for Gemini).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from services.agent.llm import conversation as conversation_module
from services.agent.llm.config import MAX_TOOL_ITERATIONS, LlmSettings
from services.agent.llm.context import ConversationState
from services.agent.llm.conversation import UsageTotals, generate_reply
from services.agent.llm.errors import (
    DailySpendCapExceededError,
    TokenSpendCapExceededError,
    ToolLoopLimitError,
    TurnCapExceededError,
    read_usage_so_far,
)
from services.agent.llm.model_types import (
    ModelResponse,
    ModelTurn,
    ModelUsage,
    ToolCall,
    ToolResultTurn,
    Turn,
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
    responses: list[ModelResponse]
    calls: list[list[Turn]] = field(default_factory=list)

    async def generate(
        self, *, turns: list[Turn], system_instruction: str
    ) -> ModelResponse:
        del system_instruction  # unused: this fake only records `turns`
        self.calls.append(list(turns))
        return self.responses.pop(0)


def _text_response(text: str, *, total_tokens: int = 10) -> ModelResponse:
    return ModelResponse(
        turn=ModelTurn(text=text, tool_calls=()),
        usage=ModelUsage(
            prompt_tokens=total_tokens - 2,
            candidates_tokens=2,
            total_tokens=total_tokens,
        ),
    )


def _function_call_response(
    name: str,
    args: dict[str, Any],
    *,
    total_tokens: int = 12,
    call_id: str = "call_0",
) -> ModelResponse:
    return ModelResponse(
        turn=ModelTurn(
            text=None, tool_calls=(ToolCall(id=call_id, name=name, args=args),)
        ),
        usage=ModelUsage(
            prompt_tokens=total_tokens - 2,
            candidates_tokens=2,
            total_tokens=total_tokens,
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
    # The model's tool-call turn and our tool-result turn were both
    # appended before the second model call.
    assert len(transport.calls) == 2
    second_call_turns = transport.calls[1]
    assert len(second_call_turns) == 2
    assert isinstance(second_call_turns[0], ModelTurn)
    assert second_call_turns[0].tool_calls[0].name == "get_quote"
    assert isinstance(second_call_turns[1], ToolResultTurn)
    assert second_call_turns[1].results[0].name == "get_quote"
    assert second_call_turns[1].results[0].result == fake_result


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
        _conn: Any, *, conversation_id: int, now: Any, settings: Any, usage_so_far: Any
    ) -> None:
        del conversation_id, now, settings, usage_so_far
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
        _conn: Any, *, conversation_id: int, now: Any, settings: Any, usage_so_far: Any
    ) -> None:
        del conversation_id, now, settings, usage_so_far
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


def test_generate_reply_rechecks_the_spend_cap_before_every_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of re-checking mid-loop is that the check must see
    usage accumulated during the current turn -- not only what
    check_token_spend_caps itself would read from token_usage, which
    generate_reply never writes to (see the module docstring). This test
    proves the wiring: generate_reply threads its running UsageTotals into
    the check on every iteration, by recording exactly what usage_so_far
    each call received. A fake stands in for check_token_spend_caps here
    (real SUM-query/threshold arithmetic is covered by
    tests/integration/test_llm_caps.py) so this stays a fast, no-database
    test of generate_reply's own orchestration."""
    _stub_conversation_state(monkeypatch, turn_count=0)
    seen_usage_so_far: list[UsageTotals] = []

    def _check(
        _conn: Any,
        *,
        conversation_id: int,
        now: Any,
        settings: Any,
        usage_so_far: UsageTotals,
    ) -> None:
        del conversation_id, now, settings
        seen_usage_so_far.append(usage_so_far)
        if usage_so_far.total_tokens >= 20:
            raise TokenSpendCapExceededError("conversation 1 crossed its cap mid-turn")

    monkeypatch.setattr(conversation_module, "check_token_spend_caps", _check)
    monkeypatch.setattr(
        conversation_module,
        "dispatch_tool",
        lambda _conn, _name, _args, **_kwargs: {"available": False},
    )
    # Each call reports 12 tokens; the fake cap trips at 20, so it must
    # cross between the second and third calls (0, then 12, then 24).
    responses = [
        _function_call_response("check_availability", {"hotel_id": 1}),
        _function_call_response("check_availability", {"hotel_id": 1}),
    ]
    transport = FakeTransport(responses)

    with pytest.raises(TokenSpendCapExceededError) as exc_info:
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

    assert [usage.total_tokens for usage in seen_usage_so_far] == [0, 12, 24]
    # Exactly 2 real model calls happened -- the loop stopped at the
    # crossing point (the 3rd iteration's pre-check), not after exhausting
    # MAX_TOOL_ITERATIONS and not after a 3rd call.
    assert len(transport.calls) == 2
    # generate_reply's own wrapping try/except attaches usage_so_far to
    # whatever the loop raised -- the fake above no longer needs to (and
    # does not) set it itself.
    attached = read_usage_so_far(exc_info.value)
    assert attached is not None
    assert attached.total_tokens == 24
