"""Exception types raised by services/agent/llm.

Mirrors services/pricing/errors.py and services/inventory/errors.py's
pattern — every exception maps to a specific, expected failure mode, never
a bare Exception, per CLAUDE.md §2.
"""

from __future__ import annotations


class LlmError(Exception):
    """Base class for every exception this package raises."""


class ConversationNotFoundError(LlmError):
    """Raised when generate_reply is called with a conversation_id that
    does not exist in the conversations table.

    The caller is expected to have created the conversation row before
    ever calling into this module — this module only reads conversation
    state (ARCHITECTURE.md §7), never creates it.
    """


class LlmConfigurationError(LlmError):
    """Raised when the environment does not describe a usable, reviewed
    model configuration — an unset or unrecognized LLM_MODEL, a missing
    API key, or an invalid numeric setting.

    Raised at settings-load time, not at the first customer message:
    CLAUDE.md §9 requires the exact model version to be pinned and
    reviewed, so a bad configuration must fail startup, not silently fall
    back to a default.
    """


class ModelUnavailableError(LlmError):
    """Raised when the model transport itself fails — a timeout, a
    network error, or any other SDK-level failure.

    CLAUDE.md §8: every external call has a timeout and explicit failure
    handling. The caller is expected to treat this as "the agent could
    not respond this turn", not to retry silently inside this module.
    """


class UnknownToolError(LlmError):
    """Raised when the model calls a tool name that is not one of the
    declarations this module sent it.

    This is not expected to happen with a well-behaved model against the
    tool declarations in tools.py, but the dispatch layer must never
    execute a name it does not recognize.
    """


class InvalidToolArgumentsError(LlmError):
    """Raised when a tool call's arguments do not match what the
    underlying service function requires — missing keys, wrong types, or
    values the service itself would reject (e.g. check_out on or before
    check_in).

    The model produced these arguments; malformed ones are an expected
    failure mode of a function-calling model, not a bug in this code.
    """


class TurnCapExceededError(LlmError):
    """Raised when a conversation has already reached
    MAX_CONVERSATION_TURNS (CLAUDE.md §9: "cap conversation turns; beyond
    the cap, escalate to a human").

    Raised before any model call is made for the turn — the caller is
    expected to open a human escalation instead of calling generate_reply
    again for this conversation.
    """


class ToolLoopLimitError(LlmError):
    """Raised when a single customer turn drives more tool calls than
    MAX_TOOL_ITERATIONS without the model producing a final text reply.

    A well-formed exchange with two read-only tools resolves in at most a
    couple of calls; exceeding the limit means something is wrong (the
    model is stuck retrying, or arguments keep coming back invalid) and
    burning further tokens on it is a spend risk (CLAUDE.md §9's per-
    conversation token cap), not a case worth retrying further.
    """
