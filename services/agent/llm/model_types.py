"""Provider-neutral request/response types for services/agent/llm —
CLAUDE.md §9: "the model is accessed through one interface module." This
module is the vocabulary the rest of services/agent/llm/ speaks —
tools.py, context.py, conversation.py — so that services/agent/llm/
client.py is the only place that ever imports a specific provider's SDK
or knows its wire format. Adding a second transport (a different
provider, or a fallback model from a different provider) should never
require touching anything outside client.py's own translation code.

ModelTurn is round-tripped: it is what a model call returns, and it is
also what gets appended back into the next call's turn history unchanged
— the same requirement both Gemini (candidates[0].content must precede a
function-response turn) and OpenAI-compatible APIs (the assistant
message, with its own tool_calls field, must precede a role="tool"
message) impose, just with different wire shapes. Keeping ModelTurn
provider-neutral is what lets client.py satisfy either requirement from
the same value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolDeclaration:
    """One tool the model may call. parameters is plain JSON Schema —
    the one representation every provider's own tool-calling format
    (Gemini's types.Schema, an OpenAI-compatible API's function schema)
    can be built from mechanically, so tools.py stays the one file every
    declaration lives in (CLAUDE.md §9) without also being shaped like
    one specific SDK's objects.
    """

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """One function call the model asked for. id is always populated
    (synthesized from position if a provider's SDK does not supply one,
    e.g. Gemini today, which correlates by order instead) — a stable
    identifier every provider's ToolResult can key off of uniformly,
    since an OpenAI-compatible API's tool_call_id is load-bearing where
    Gemini's is not."""

    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """One executed tool call's result, ready to send back to the model."""

    call_id: str
    name: str
    result: dict[str, Any]


@dataclass(frozen=True)
class UserTurn:
    """One customer message, from conversation history."""

    text: str


@dataclass(frozen=True)
class ModelTurn:
    """One turn the model itself produced — text, one or more tool
    calls, or (rarely, per the underlying APIs' own shapes) both. A
    turn loaded from message history (services.agent.llm.context) is
    always tool_calls=() — this codebase does not store tool-call
    history, only the final text of a past reply.

    provider_state is opaque outside the transport that produced it —
    conversation.py never reads or constructs it, only carries it along
    (this turn gets appended back into the next call's turns list
    unchanged). It exists so a transport can preserve whatever
    provider-specific continuity data its own API needs echoed back
    verbatim (e.g. Gemini's thought_signature, attached to reasoning
    continuity across a tool-calling turn) without that concern leaking
    into this supposedly-neutral type's other fields, or into any other
    module. A turn built from plain text (message history, or a second
    transport with no such concept) simply leaves it None; a transport
    that receives a ModelTurn with a provider_state it recognizes as its
    own may use it directly instead of reconstructing the turn from
    text/tool_calls, and must fall back to reconstructing when it does
    not (a turn produced by a *different* transport, or loaded from
    history) — never assume the field's shape is its own.
    """

    text: str | None
    tool_calls: tuple[ToolCall, ...]
    provider_state: Any = None


@dataclass(frozen=True)
class ToolResultTurn:
    """The results of one or more tool calls, sent back to the model.
    Always immediately follows the ModelTurn whose calls it answers."""

    results: tuple[ToolResult, ...]


Turn = UserTurn | ModelTurn | ToolResultTurn


@dataclass(frozen=True)
class ModelUsage:
    """One model call's token usage — provider-neutral names for the
    same three figures services.agent.llm.conversation.UsageTotals
    accumulates across a turn's calls (that type is unchanged; this is
    just what a single call reports)."""

    prompt_tokens: int
    candidates_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class ModelResponse:
    """One model call's result: the turn it produced, plus that turn's
    own usage."""

    turn: ModelTurn
    usage: ModelUsage
