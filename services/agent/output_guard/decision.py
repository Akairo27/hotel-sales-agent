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

Two more reasons sit alongside the value check, both currency-shaped
rather than value-shaped — prompt.py's prices_are_saudi_riyals_only and
price_currency_word are the corresponding prompt-side rules, but the
guard exists to hold even if the model ignores them:

- AMOUNT_FOREIGN_CURRENCY: a candidate adjacent to a foreign-currency
  marker (extraction.CandidateAmount.foreign_currency_marker) blocks
  outright, regardless of whether its halalas value happens to equal a
  real amount — "1,350.00 USD" is the real total with the wrong label,
  and a right number in the wrong currency is exactly what this guard
  must not let through.
- AMOUNT_NO_CURRENCY_MARKER: a candidate that matches a real amount but
  carries no marker at all — neither SAR nor foreign — blocks instead of
  passing as a clean match. Enumerating every foreign currency can never
  be complete ("1,350.00 złoty"), and a bare "1,350.00" with no currency
  word is the same hole spelled differently; requiring the one currency
  this system actually uses closes both without an enumeration.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.agent.output_guard.extraction import (
    CandidateAmount,
    extract_candidate_amounts,
)

AMOUNT_MATCHED = "matched"
AMOUNT_NOT_IN_QUOTES = "not_in_quotes"
AMOUNT_BELOW_FLOOR = "below_floor"
AMOUNT_UNPARSEABLE = "unparseable"
AMOUNT_FOREIGN_CURRENCY = "foreign_currency"
AMOUNT_NO_CURRENCY_MARKER = "no_currency_marker"

BLOCKING_AMOUNT_REASONS = frozenset(
    {
        AMOUNT_NOT_IN_QUOTES,
        AMOUNT_BELOW_FLOOR,
        AMOUNT_UNPARSEABLE,
        AMOUNT_FOREIGN_CURRENCY,
        AMOUNT_NO_CURRENCY_MARKER,
    }
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


def _reason_for_matched_candidate(candidate: CandidateAmount) -> str:
    """The reason for a candidate whose value matches a real amount.

    A match with no currency marker at all — neither SAR nor foreign —
    is not a clean AMOUNT_MATCHED: see the module docstring for why an
    unmarked right number is its own hole, distinct from a wrongly
    labelled one. foreign_currency_marker is never set here (that case
    is handled earlier in evaluate_amounts, before a value match is even
    checked), so only the marker-less case needs deciding.
    """
    if candidate.has_currency_marker:
        return AMOUNT_MATCHED
    return AMOUNT_NO_CURRENCY_MARKER


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
        if candidate.foreign_currency_marker is not None:
            findings.append(
                AmountFinding(candidate.raw, candidate.halalas, AMOUNT_FOREIGN_CURRENCY)
            )
            continue
        if candidate.halalas in allowed.amounts_halalas:
            findings.append(
                AmountFinding(
                    candidate.raw,
                    candidate.halalas,
                    _reason_for_matched_candidate(candidate),
                )
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
