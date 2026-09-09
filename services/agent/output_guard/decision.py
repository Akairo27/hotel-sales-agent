"""Decides whether a candidate reply's stated amounts are legitimate for
a conversation — the second stage of the output guard (ARCHITECTURE.md
§7, CLAUDE.md rule 8).

Pure: takes an already-known set of legitimate amounts and never touches
a database itself (see quotes.py for that read). This separation is what
lets the required adversarial test corpus (CLAUDE.md §6,
tests/adversarial/) exercise the actual blocking logic deterministically,
without a live model or a database.

An amount is legitimate only if it exactly equals a value
quote_to_tool_result (services/agent/llm/dispatch.py) actually sent the
model for this conversation — never a value derived from them by
arithmetic (a sum, a per-room subtotal, a rounding), even if that
arithmetic would itself be correct. min_allowed is deliberately never a
legitimate amount: the model was never shown it, so a reply stating it
verbatim is a floor leak, not a legitimate echo — it is what makes
AMOUNT_BELOW_FLOOR distinguishable from AMOUNT_NOT_IN_QUOTES at all,
since every genuinely allowed amount is already >= its own quote's floor
by the quotes_min_allowed_not_above_ask database constraint.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.agent.output_guard.extraction import extract_candidate_amounts

AMOUNT_MATCHED = "matched"
AMOUNT_NOT_IN_QUOTES = "not_in_quotes"
AMOUNT_BELOW_FLOOR = "below_floor"
AMOUNT_UNPARSEABLE = "unparseable"

BLOCKING_AMOUNT_REASONS = frozenset(
    {AMOUNT_NOT_IN_QUOTES, AMOUNT_BELOW_FLOOR, AMOUNT_UNPARSEABLE}
)


@dataclass(frozen=True)
class AllowedAmounts:
    """The complete set of amounts (in halalas) a reply may legitimately
    state for one conversation, and the floor no amount may fall below.

    quote_ids names every quote this came from, for the escalation
    record only — matching is against amounts_halalas as a whole, never
    against one quote at a time, since a conversation can legitimately
    reference more than one quote (different dates asked about in the
    same thread).
    """

    quote_ids: tuple[int, ...]
    amounts_halalas: frozenset[int]
    floor_halalas: int | None  # None only when the conversation has no quotes


@dataclass(frozen=True)
class AmountFinding:
    """The verdict for one candidate amount found in a reply's text."""

    raw: str
    halalas: int | None
    reason: str


def evaluate_amounts(text: str, allowed: AllowedAmounts) -> tuple[AmountFinding, ...]:
    """Checks every candidate financial amount in text against allowed.

    Returns one AmountFinding per candidate amount found in text (empty
    if none were found at all — a reply with no stated price always
    passes). Callers decide overall pass/fail via amounts_are_allowed;
    this function reports every offending amount, not just the first, so
    an escalation record can show the whole picture.
    """
    findings: list[AmountFinding] = []
    for candidate in extract_candidate_amounts(text):
        if candidate.halalas is None:
            findings.append(AmountFinding(candidate.raw, None, AMOUNT_UNPARSEABLE))
            continue
        if candidate.halalas in allowed.amounts_halalas:
            findings.append(
                AmountFinding(candidate.raw, candidate.halalas, AMOUNT_MATCHED)
            )
            continue
        if (
            allowed.floor_halalas is not None
            and candidate.halalas < allowed.floor_halalas
        ):
            findings.append(
                AmountFinding(candidate.raw, candidate.halalas, AMOUNT_BELOW_FLOOR)
            )
            continue
        findings.append(
            AmountFinding(candidate.raw, candidate.halalas, AMOUNT_NOT_IN_QUOTES)
        )
    return tuple(findings)


def amounts_are_allowed(findings: tuple[AmountFinding, ...]) -> bool:
    """Whether every finding matched a legitimate amount — i.e. whether
    the reply may be sent as-is."""
    return all(finding.reason == AMOUNT_MATCHED for finding in findings)
