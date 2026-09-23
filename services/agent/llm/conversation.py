"""The tool-calling loop: one customer turn in, one candidate reply out.

generate_reply is the single public entry point CLAUDE.md §9 asks for —
"the model is accessed through one interface module." It reads
conversation state and the last MESSAGE_WINDOW messages (never the full
history — ARCHITECTURE.md §7), calls the model, executes any tool calls
through dispatch.py, and returns an AgentReply. It never writes to
conversations or messages, and it never sends anything to a customer —
both belong to the not-yet-built webhook.

Everything in this module speaks services.agent.llm.model_types, the
provider-neutral vocabulary — never a specific provider's SDK types.
services.agent.llm.client is the only place that translates to and from
whichever provider a given ModelTransport actually calls.

Spend caps are checked, not written. check_token_spend_caps
(services.agent.llm.caps) runs before every transport.generate() call
inside the tool-calling loop below — not just once before the loop — so
a conversation sitting just under its cap cannot ride out up to
MAX_TOOL_ITERATIONS model calls in one turn before the cap is next
consulted. Each check is passed `usage`, the running UsageTotals
accumulated by this turn's own calls so far: generate_reply never inserts
into token_usage itself (recording is the webhook's job, once the whole
turn ends — services.agent.llm.caps.record_token_usage), so without
threading `usage` in, a same-turn recheck would only ever see what was
already committed before the turn started and would catch nothing new.

The loop (and its own MAX_TOOL_ITERATIONS-exhausted ToolLoopLimitError) is
wrapped in exactly one try/except, which attaches the loop's current
`usage` to whatever exception it raised via errors.attach_usage_so_far,
then re-raises it unchanged. This is deliberately the only place that
attachment happens: every exception the loop can produce -- the two cap
errors above, UsageUnavailableError, ModelUnavailableError,
UnknownToolError, InvalidToolArgumentsError, ToolLoopLimitError, and any
pricing exception dispatch.py lets propagate -- can follow real,
already-paid-for model calls, and the caller (webhook.py) needs a single,
type-agnostic way to ask "was there usage to record before this turn
died," not a growing list of exception-specific cases to remember.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg

from lib.hijri import to_hijri
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
    attach_usage_so_far,
)
from services.agent.llm.model_types import ModelUsage, ToolResult, ToolResultTurn, Turn
from services.agent.llm.pricing import riyadh_calendar_day
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

    @staticmethod
    def from_model_usage(usage: ModelUsage) -> UsageTotals:
        """Provider-neutral ModelUsage (one call) -> this module's own
        UsageTotals (accumulated across a turn's calls) -- same three
        figures, different type, so a turn's running total and one
        call's usage can never be confused for each other."""
        return UsageTotals(
            prompt_tokens=usage.prompt_tokens,
            candidates_tokens=usage.candidates_tokens,
            total_tokens=usage.total_tokens,
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
        ConversationNotFoundError: conversation_id does not exist. Raised
            before the tool-calling loop, before any model call — never
            carries usage_so_far (see errors.read_usage_so_far).
        TurnCapExceededError: the conversation has already reached
            settings.max_conversation_turns (CLAUDE.md §9) — raised
            before any model call, same as ConversationNotFoundError
            above; the caller must escalate instead of calling this again
            for this conversation.
        TokenSpendCapExceededError: this conversation's logged token
            usage, plus this turn's own usage so far, has reached
            settings.max_tokens_per_conversation. Checked before every
            model call in the tool-calling loop below, not only the
            first — so this can follow one or more real model calls
            already made in this same turn.
        DailySpendCapExceededError: today's (Asia/Riyadh calendar day)
            estimated spend across every conversation, plus this turn's
            own usage so far, has reached settings.max_spend_per_day_usd.
            Same mid-turn timing as TokenSpendCapExceededError above.
        UsageUnavailableError: a model response carried no usable
            token-usage data — see the active transport's own
            translation code (e.g. services.agent.llm.client's
            _gemini_response_to_model_response).
        ToolLoopLimitError: the model kept calling tools past
            MAX_TOOL_ITERATIONS without producing a final reply.
        ModelUnavailableError: the model transport failed.
        UnknownToolError, InvalidToolArgumentsError: see dispatch.py.
        Any exception services.pricing.compute_quote raises for a genuine
            pricing misconfiguration (see dispatch.py's module docstring)
            — deliberately left to propagate, not caught here.

        Every exception above except the first two (ConversationNotFound-
        Error, TurnCapExceededError) can be raised after one or more real
        model calls already happened in this same turn, and carries that
        turn's usage-so-far — retrievable via errors.read_usage_so_far,
        regardless of the exception's specific type — so the caller can
        record it before treating the turn as failed. See this module's
        own docstring for the single wrapping mechanism that makes this
        uniform across every exception the loop below can produce.
    """
    state = load_conversation_state(conn, conversation_id)
    if state.turn_count >= settings.max_conversation_turns:
        raise TurnCapExceededError(
            f"conversation {conversation_id} is at its turn cap "
            f"({settings.max_conversation_turns})"
        )

    messages = load_recent_messages(conn, conversation_id, limit=MESSAGE_WINDOW)
    turns: list[Turn] = build_contents(messages)
    today = riyadh_calendar_day(now)
    system_instruction = render_system_instruction(
        customer_name=customer_name, today=today, today_hijri=to_hijri(today)
    )

    tool_calls: list[ToolCallRecord] = []
    quote_ids: list[int] = []
    usage = UsageTotals.zero()

    try:
        for _ in range(MAX_TOOL_ITERATIONS):
            check_token_spend_caps(
                conn,
                conversation_id=conversation_id,
                now=now,
                settings=settings,
                usage_so_far=usage,
            )
            response = await transport.generate(
                turns=turns, system_instruction=system_instruction
            )
            usage = usage + UsageTotals.from_model_usage(response.usage)

            model_turn = response.turn
            if not model_turn.tool_calls:
                return AgentReply(
                    text=model_turn.text or "",
                    tool_calls=tuple(tool_calls),
                    quote_ids=tuple(quote_ids),
                    usage=usage,
                )

            turns.append(model_turn)

            results: list[ToolResult] = []
            for call in model_turn.tool_calls:
                result = dispatch_tool(
                    conn,
                    call.name,
                    call.args,
                    now=now,
                    customer_phone=state.customer_phone,
                    conversation_id=state.id,
                )
                tool_calls.append(
                    ToolCallRecord(name=call.name, args=call.args, result=result)
                )
                if result.get("priced") is True:
                    quote_ids.append(int(result["quote_id"]))
                results.append(
                    ToolResult(call_id=call.id, name=call.name, result=result)
                )
            turns.append(ToolResultTurn(results=tuple(results)))

        raise ToolLoopLimitError(
            f"conversation {conversation_id} exceeded {MAX_TOOL_ITERATIONS} tool "
            "iterations in one turn without a final reply"
        )
    except Exception as exc:
        # The one place any exception from the loop above is tagged with
        # this turn's usage-so-far -- see this module's own docstring and
        # errors.attach_usage_so_far for why this is deliberately generic
        # rather than a per-exception-type concern.
        attach_usage_so_far(exc, usage)
        raise
