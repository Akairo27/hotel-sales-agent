"""Exception types raised by services/agent/llm.

Mirrors services/pricing/errors.py and services/inventory/errors.py's
pattern — every exception maps to a specific, expected failure mode, never
a bare Exception, per CLAUDE.md §2.

attach_usage_so_far / read_usage_so_far (below the exception classes) are
the one write site and one read site for a different concern: carrying a
turn's already-spent-but-not-yet-committed usage across whatever exception
generate_reply's tool-calling loop happens to raise. Deliberately generic
rather than a constructor parameter on each affected exception class: the
loop can raise exceptions this module doesn't even define (pricing
misconfigurations live in services.pricing.errors, a different hierarchy
entirely), so the only mechanism that reaches all of them is attaching the
value to whatever instance was actually raised, regardless of its type.
Giving every "this can happen mid-turn, after real spend" exception its
own __init__ override would recreate exactly the per-exception-type
bookkeeping this design exists to avoid.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from services.agent.llm.conversation import UsageTotals

USAGE_SO_FAR_ATTR: Final[str] = "usage_so_far"


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
    earlier in the same turn. Instances raised from within that loop carry
    the turn's usage-so-far via attach_usage_so_far/read_usage_so_far
    (below) — not a constructor parameter on this class, since the same
    carrying mechanism must work uniformly for every exception the loop
    can raise, including ones this module doesn't define.

    The caller is expected to open a human escalation instead of calling
    generate_reply again for this conversation.
    """


class DailySpendCapExceededError(LlmError):
    """Raised when today's (Asia/Riyadh calendar day) total estimated
    spend across every conversation — committed spend plus usage_so_far
    from the current turn's own model calls — has reached
    settings.max_spend_per_day_usd or more. A global backstop, not scoped
    to one conversation.

    Carries the turn's usage-so-far for the same reason and via the same
    mechanism as TokenSpendCapExceededError — see that class's docstring.

    This is a soft cap: the check is a SUM query against token_usage, not
    a lock, so a small overshoot under concurrent load right at the
    boundary is possible and accepted (CLAUDE.md §9's "cap token spend...
    per day", read as a backstop against a runaway cost day rather than a
    financial-loss-grade constraint like inventory overselling).
    """


class UsageUnavailableError(LlmError):
    """Raised when a model response carried no usable token-usage data —
    usage_metadata was absent, or one of its counts was None.

    Raised instead of silently treating the call as free: a cap enforced
    against an undercounted total isn't a cap. Propagates uncaught through
    generate_reply, same as a pricing misconfiguration — and, like every
    other exception generate_reply's tool-calling loop can raise, carries
    the turn's usage-so-far via attach_usage_so_far/read_usage_so_far
    (below). The webhook is the catcher: the model call already happened
    (real spend) by the time this is raised, so it logs at ERROR and
    returns 200 rather than retrying — see services/agent/webhook.py's
    module docstring for why a 500 here would be worse, not safer.
    """


def attach_usage_so_far(exc: BaseException, usage: UsageTotals) -> None:
    """The single write site for cross-exception usage-carrying — pairs
    with read_usage_so_far below. Both live here, next to
    USAGE_SO_FAR_ATTR, so the attribute name is never duplicated as a
    literal at either call site: generate_reply's tool-calling loop
    (services/agent/llm/conversation.py) calls this from one wrapping
    try/except around the whole loop, on whatever exception it just
    caught, regardless of that exception's type or which module defined
    it.
    """
    setattr(exc, USAGE_SO_FAR_ATTR, usage)


def read_usage_so_far(exc: BaseException) -> UsageTotals | None:
    """The single read site pairing with attach_usage_so_far above.
    Returns None for an exception attach_usage_so_far never touched
    (raised outside generate_reply's tool-calling loop, before any model
    call in the turn could have happened — TurnCapExceededError and
    ConversationNotFoundError are the two examples in this codebase today)
    rather than raising, since "no usage was ever attached" is an expected
    outcome for those, not a bug.
    """
    return getattr(exc, USAGE_SO_FAR_ATTR, None)
