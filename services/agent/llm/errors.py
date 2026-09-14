"""Exception types raised by services/agent/llm.

Mirrors services/pricing/errors.py and services/inventory/errors.py's
pattern — every exception maps to a specific, expected failure mode, never
a bare Exception, per CLAUDE.md §2.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.agent.llm.conversation import UsageTotals


class LlmError(Exception):
    """Base class for every exception this package raises."""


class ConversationNotFoundError(LlmError):
    """Raised when generate_reply is called with a conversation_id that
    does not exist in the conversations table.

    The caller is expected to have created the conversation row before
    ever calling into this module — this module only reads conversation
    state (ARCHITECTURE.md §7), never creates it.

    Reused by services.agent.output_guard.enforce_outbound_text for the
    same underlying condition (its escalation-opening INSERT ... SELECT
    matches zero rows) rather than defining a second class for the same
    meaning — CLAUDE.md §2's "one way to do each thing."
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


class TokenSpendCapExceededError(LlmError):
    """Raised when a conversation's total token usage — committed usage
    from token_usage plus usage_so_far from the current turn's own model
    calls — has reached settings.max_tokens_per_conversation or more.

    No longer raised only before any model call: conversation.py
    re-checks this cap before every transport.generate() call inside its
    tool-calling loop (not just once before the loop), so this can now be
    raised after one or more real, already-paid-for model calls happened
    earlier in the same turn. usage_so_far carries exactly that
    already-spent usage so the caller (webhook.py) can record it before
    discarding the turn as capped — dropping it here would silently lose
    real spend the same way an unhandled UsageUnavailableError would (see
    webhook.py's module docstring).

    The caller is expected to open a human escalation instead of calling
    generate_reply again for this conversation.
    """

    def __init__(self, message: str, *, usage_so_far: UsageTotals) -> None:
        super().__init__(message)
        self.usage_so_far = usage_so_far


class DailySpendCapExceededError(LlmError):
    """Raised when today's (Asia/Riyadh calendar day) total estimated
    spend across every conversation — committed spend plus usage_so_far
    from the current turn's own model calls — has reached
    settings.max_spend_per_day_usd or more. A global backstop, not scoped
    to one conversation.

    Carries usage_so_far for the same reason and under the same
    now-possible-mid-turn timing as TokenSpendCapExceededError — see that
    class's docstring.

    This is a soft cap: the check is a SUM query against token_usage, not
    a lock, so a small overshoot under concurrent load right at the
    boundary is possible and accepted (CLAUDE.md §9's "cap token spend...
    per day", read as a backstop against a runaway cost day rather than a
    financial-loss-grade constraint like inventory overselling).
    """

    def __init__(self, message: str, *, usage_so_far: UsageTotals) -> None:
        super().__init__(message)
        self.usage_so_far = usage_so_far


class UsageUnavailableError(LlmError):
    """Raised when a model response carried no usable token-usage data —
    usage_metadata was absent, or one of its counts was None.

    Raised instead of silently treating the call as free: a cap enforced
    against an undercounted total isn't a cap. Propagates uncaught through
    generate_reply, same as a pricing misconfiguration. The webhook is the
    catcher: the model call already happened (real spend) by the time this
    is raised, so it logs at ERROR and returns 200 rather than retrying —
    see services/agent/webhook.py's module docstring for why a 500 here
    would be worse, not safer.
    """
