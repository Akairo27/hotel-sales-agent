"""The tool-calling loop: one customer turn in, one candidate reply out.

generate_reply is the single public entry point CLAUDE.md §9 asks for —
"the model is accessed through one interface module." It reads
conversation state and the last MESSAGE_WINDOW messages (never the full
history — ARCHITECTURE.md §7), calls the model, executes any tool calls
through dispatch.py, and returns an AgentReply. It never writes to
conversations or messages, and it never sends anything to a customer —
both belong to the not-yet-built webhook.

Spend caps are checked, not written. check_token_spend_caps runs right
after the turn-cap check, before any model call, against the token_usage
log (services.agent.llm.caps) — but generate_reply never inserts into
that log itself. Recording a call's usage happens only once a reply has
passed the output guard and been sent, which is the webhook's job
(services.agent.llm.caps.record_token_usage), not this module's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from google.genai import types

from services.agent.llm.caps import check_token_spend_caps
from services.agent.llm.client import ModelTransport
from services.agent.llm.config import MAX_TOOL_ITERATIONS, MESSAGE_WINDOW, LlmSettings
from services.agent.llm.context import (
    build_contents,
    load_conversation_state,
    load_recent_messages,
)
from services.agent.llm.dispatch import dispatch_tool
from services.agent.llm.errors import (
    ToolLoopLimitError,
    TurnCapExceededError,
    UsageUnavailableError,
)
from services.agent.llm.prompt import render_system_instruction


@dataclass(frozen=True)
class ToolCallRecord:
    """One executed tool call, kept on AgentReply for logging and for the
    not-yet-built output guard — never re-decided or re-checked here."""

    name: str
    args: dict[str, Any]
    result: dict[str, Any]


@dataclass(frozen=True)
class UsageTotals:
    """Token usage accumulated across every model call in one turn."""

    prompt_tokens: int
    candidates_tokens: int
    total_tokens: int

    @staticmethod
    def zero() -> UsageTotals:
        return UsageTotals(0, 0, 0)

    def __add__(self, other: UsageTotals) -> UsageTotals:
        return UsageTotals(
            self.prompt_tokens + other.prompt_tokens,
            self.candidates_tokens + other.candidates_tokens,
            self.total_tokens + other.total_tokens,
        )


@dataclass(frozen=True)
class AgentReply:
    """The candidate reply for one customer turn.

    Deliberately inert: text plus the trail behind it, not something sent
    anywhere. The (separate PR's) output guard runs on `text` before
    anything reaches a customer — CLAUDE.md rule 8.
    """

    text: str
    tool_calls: tuple[ToolCallRecord, ...]
    quote_ids: tuple[int, ...]
    usage: UsageTotals


def _usage_from(response: types.GenerateContentResponse) -> UsageTotals:
    """Extracts one model call's token usage.

    thoughts_token_count (the SDK's separate field for extended-thinking
    tokens) is folded into candidates_tokens, not tracked on its own:
    Gemini bills thinking tokens at the output rate, and
    services.agent.llm.pricing.estimate_cost_usd's two-bucket formula
    would silently undercount any call that used extended thinking if
    this weren't added in. Unlike the three fields below, its absence is
    not a sign of a broken response — a call that used no extended
    thinking legitimately reports none — so it defaults to 0 rather than
    raising. total_token_count is left untouched: the SDK already
    includes thinking tokens in that figure, so re-adding them here would
    double-count against the per-conversation token cap.

    Raises:
        UsageUnavailableError: usage_metadata is absent, or one of its
            three required counts is None. An untelemetered call is not
            a free call — treating it as zero would let real spend go
            uncounted against every cap this module and caps.py check.
    """
    usage = response.usage_metadata
    if usage is None:
        raise UsageUnavailableError("model response carried no usage_metadata")
    if (
        usage.prompt_token_count is None
        or usage.candidates_token_count is None
        or usage.total_token_count is None
    ):
        raise UsageUnavailableError("model response usage_metadata is incomplete")
    candidates_tokens = usage.candidates_token_count + (usage.thoughts_token_count or 0)
    return UsageTotals(
        prompt_tokens=usage.prompt_token_count,
        candidates_tokens=candidates_tokens,
        total_tokens=usage.total_token_count,
    )


async def generate_reply(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    customer_name: str | None,
    transport: ModelTransport,
    settings: LlmSettings,
    now: datetime,
) -> AgentReply:
    """Generates the candidate reply for a conversation's next turn.

    Raises:
        ConversationNotFoundError: conversation_id does not exist.
        TurnCapExceededError: the conversation has already reached
            settings.max_conversation_turns (CLAUDE.md §9) — raised
            before any model call; the caller must escalate instead of
            calling this again for this conversation.
        TokenSpendCapExceededError: this conversation's logged token
            usage has already reached settings.max_tokens_per_conversation
            — raised before any model call, same as the turn cap.
        DailySpendCapExceededError: today's (Asia/Riyadh calendar day)
            estimated spend across every conversation has already
            reached settings.max_spend_per_day_usd.
        UsageUnavailableError: a model response carried no usable
            token-usage data — see _usage_from.
        ToolLoopLimitError: the model kept calling tools past
            MAX_TOOL_ITERATIONS without producing a final reply.
        ModelUnavailableError: the model transport failed.
        UnknownToolError, InvalidToolArgumentsError: see dispatch.py.
        Any exception services.pricing.compute_quote raises for a genuine
            pricing misconfiguration (see dispatch.py's module docstring)
            — deliberately left to propagate, not caught here.
    """
    state = load_conversation_state(conn, conversation_id)
    if state.turn_count >= settings.max_conversation_turns:
        raise TurnCapExceededError(
            f"conversation {conversation_id} is at its turn cap "
            f"({settings.max_conversation_turns})"
        )
    check_token_spend_caps(
        conn, conversation_id=conversation_id, now=now, settings=settings
    )

    messages = load_recent_messages(conn, conversation_id, limit=MESSAGE_WINDOW)
    contents = build_contents(messages)
    system_instruction = render_system_instruction(customer_name=customer_name)

    tool_calls: list[ToolCallRecord] = []
    quote_ids: list[int] = []
    usage = UsageTotals.zero()

    for _ in range(MAX_TOOL_ITERATIONS):
        response = await transport.generate(
            contents=contents, system_instruction=system_instruction
        )
        usage = usage + _usage_from(response)

        calls = response.function_calls
        if not calls:
            return AgentReply(
                text=response.text or "",
                tool_calls=tuple(tool_calls),
                quote_ids=tuple(quote_ids),
                usage=usage,
            )

        candidates = response.candidates
        if candidates:
            model_content = candidates[0].content
            if model_content is not None:
                contents.append(model_content)

        response_parts: list[types.Part] = []
        for call in calls:
            name = call.name or ""
            args = call.args or {}
            result = dispatch_tool(
                conn,
                name,
                args,
                now=now,
                customer_phone=state.customer_phone,
                conversation_id=state.id,
            )
            tool_calls.append(ToolCallRecord(name=name, args=args, result=result))
            if result.get("priced") is True:
                quote_ids.append(int(result["quote_id"]))
            response_parts.append(
                types.Part.from_function_response(name=name, response=result)
            )
        contents.append(types.Content(role="user", parts=response_parts))

    raise ToolLoopLimitError(
        f"conversation {conversation_id} exceeded {MAX_TOOL_ITERATIONS} tool "
        "iterations in one turn without a final reply"
    )
