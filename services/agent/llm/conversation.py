"""The tool-calling loop: one customer turn in, one candidate reply out.

generate_reply is the single public entry point CLAUDE.md §9 asks for —
"the model is accessed through one interface module." It reads
conversation state and the last MESSAGE_WINDOW messages (never the full
history — ARCHITECTURE.md §7), calls the model, executes any tool calls
through dispatch.py, and returns an AgentReply. It never writes to
conversations or messages, and it never sends anything to a customer —
both belong to the not-yet-built webhook.

*** Spend caps are surfaced, not enforced. *** AgentReply.usage reports
token counts for this call; nothing here accumulates them across calls or
refuses based on them. CLAUDE.md §9 requires a per-conversation and
per-number-per-day token/spend cap (.env.example's
LLM_MAX_TOKENS_PER_CONVERSATION / LLM_MAX_SPEND_PER_DAY_USD);
enforcing it needs a persisted counter, which belongs with whichever PR
wires the WhatsApp webhook. **The webhook must not merge until that
enforcement exists** — it is what first lets an unbounded stranger spend
the client's model budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from google.genai import types

from services.agent.llm.client import ModelTransport
from services.agent.llm.config import MAX_TOOL_ITERATIONS, MESSAGE_WINDOW, LlmSettings
from services.agent.llm.context import (
    build_contents,
    load_conversation_state,
    load_recent_messages,
)
from services.agent.llm.dispatch import dispatch_tool
from services.agent.llm.errors import ToolLoopLimitError, TurnCapExceededError
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
    usage = response.usage_metadata
    if usage is None:
        return UsageTotals.zero()
    return UsageTotals(
        prompt_tokens=usage.prompt_token_count or 0,
        candidates_tokens=usage.candidates_token_count or 0,
        total_tokens=usage.total_token_count or 0,
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
