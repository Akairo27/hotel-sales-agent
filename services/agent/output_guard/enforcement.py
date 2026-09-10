"""Enforces the output guard on one candidate reply — the single public
entry point ARCHITECTURE.md §7 and CLAUDE.md rule 8 describe: "never add
a code path that sends text to a customer without passing through it."

Checks the text and, on failure, opens the escalation and logs the
incident in the same call: a two-call API (decide, then separately
record) risks the worst possible failure mode here — a blocked message
with nobody ever alerted — so enforce_outbound_text guarantees
``not result.allowed => result.escalation_id is not None``.

Takes ``text``, not an AgentReply, so any future non-LLM outbound text (a
canned template, a manual reply) goes through the exact same function —
coupling this to AgentReply would leave those paths outside the guard by
construction.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import psycopg

from services.agent.llm.errors import ConversationNotFoundError
from services.agent.output_guard.decision import (
    AMOUNT_BELOW_FLOOR,
    AMOUNT_FOREIGN_CURRENCY,
    AMOUNT_MATCHED,
    AMOUNT_NO_CURRENCY_MARKER,
    AMOUNT_NOT_IN_QUOTES,
    AmountFinding,
    amounts_are_allowed,
    evaluate_amounts,
)
from services.agent.output_guard.quotes import load_allowed_amounts

logger = logging.getLogger(__name__)

# escalations.reason is free text (migration 0024's own comment names
# "an output-guard violation" as one of exactly two anticipated values,
# since the agent's full tool surface — and therefore the full set of
# escalation reasons — did not exist yet when that migration was
# written) — these four distinguish the cases a human must act on
# differently: a malformed amount is likely the model mangling a real
# number; a well-formed but wrong one is a concrete wrong price; a
# foreign-labelled one may carry the *correct* price under the wrong
# name, so staff re-send in riyals rather than investigate a wrong
# amount; a missing-currency one means the model stated the right number
# but dropped the required currency word — a prompt-compliance problem,
# not a pricing one.
REASON_UNPARSEABLE = "output_guard_violation_unparseable"
REASON_MISMATCH = "output_guard_violation_mismatch"
REASON_FOREIGN_CURRENCY = "output_guard_violation_foreign_currency"
REASON_MISSING_CURRENCY = "output_guard_violation_missing_currency"

_MISMATCH_REASONS = frozenset({AMOUNT_NOT_IN_QUOTES, AMOUNT_BELOW_FLOOR})

# Recorded in every blocked escalation's notes, not just in a code
# comment, so the retention gap is visible to whoever reviews an actual
# incident, not only to whoever reads this source file.
_RETENTION_NOTE = (
    "blocked_reply_text has no enforced retention yet. The period is the "
    "client's decision (ARCHITECTURE.md §10's reasoning for "
    "conversations/messages retention applies here too), not a technical "
    "default. Once decided, a worker analogous to "
    "services/worker/hold_expiry.py should strip blocked_reply_text (or "
    "this whole notes value) from any escalations row whose opened_at "
    "predates it — opened_at already exists and needs no schema change."
)


@dataclass(frozen=True)
class GuardVerdict:
    """The outcome of running the output guard on one candidate reply.

    escalation_id is non-None exactly when allowed is False — guaranteed
    by enforce_outbound_text, not merely a convention callers must
    remember to uphold.
    """

    allowed: bool
    findings: tuple[AmountFinding, ...]
    quote_ids: tuple[int, ...]
    escalation_id: int | None


def _escalation_reason(findings: tuple[AmountFinding, ...]) -> str:
    """Picks the single escalations.reason value for a blocked reply.

    Precedence when one reply triggers more than one kind of finding,
    most urgent first — the full per-amount detail survives regardless in
    notes["reasons"], so this only decides what a human sees first:

    1. REASON_MISMATCH (not_in_quotes / below_floor): a concrete wrong
       amount is the most urgent read regardless of what else is wrong
       with the same reply.
    2. REASON_FOREIGN_CURRENCY: more specific than "no currency at all" —
       the model actively named a currency, just the wrong one.
    3. REASON_MISSING_CURRENCY: a right number, but a prompt-compliance
       gap rather than a pricing one.
    4. REASON_UNPARSEABLE: the fallback when nothing more specific fired.
    """
    if any(finding.reason in _MISMATCH_REASONS for finding in findings):
        return REASON_MISMATCH
    if any(finding.reason == AMOUNT_FOREIGN_CURRENCY for finding in findings):
        return REASON_FOREIGN_CURRENCY
    if any(finding.reason == AMOUNT_NO_CURRENCY_MARKER for finding in findings):
        return REASON_MISSING_CURRENCY
    return REASON_UNPARSEABLE


def _open_escalation(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    reason: str,
    quote_ids: tuple[int, ...],
    findings: tuple[AmountFinding, ...],
    reply_text: str,
) -> int:
    """Inserts one escalations row for a blocked reply.

    customer_phone is copied from conversations inside this single
    INSERT ... SELECT, so it never passes through this function's own
    memory and cannot end up inside notes by mistake.

    Raises:
        ConversationNotFoundError: conversation_id does not exist (the
            SELECT matches zero rows).
    """
    notes = json.dumps(
        {
            "quote_ids": list(quote_ids),
            "blocked_amounts_halalas": [
                finding.halalas for finding in findings if finding.halalas is not None
            ],
            "reasons": [finding.reason for finding in findings],
            "blocked_reply_text": reply_text,
            "retention": _RETENTION_NOTE,
        }
    )
    row = conn.execute(
        "INSERT INTO escalations (conversation_id, customer_phone, reason, notes) "
        "SELECT id, customer_phone, %s, %s FROM conversations WHERE id = %s "
        "RETURNING id",
        (reason, notes, conversation_id),
    ).fetchone()
    if row is None:
        raise ConversationNotFoundError(
            f"conversation {conversation_id} does not exist"
        )
    return int(row[0])


def enforce_outbound_text(
    conn: psycopg.Connection[Any], *, conversation_id: int, text: str
) -> GuardVerdict:
    """Checks text against conversation_id's quotes and, if any stated
    amount does not match or falls below its floor, opens an escalation
    and logs the block before returning.

    Raises:
        ConversationNotFoundError: conversation_id does not exist. Only
            reachable via the escalation-opening path — a nonexistent
            conversation cannot have any quotes, so an amount-free reply
            against it is allowed without ever reaching the database
            write that would raise this.
    """
    allowed = load_allowed_amounts(conn, conversation_id)
    findings = evaluate_amounts(text, allowed)

    if amounts_are_allowed(findings):
        if findings:
            logger.info(
                json.dumps(
                    {
                        "event": "output_guard_pass",
                        "conversation_id": conversation_id,
                        "quote_ids": list(allowed.quote_ids),
                        "matched_amount_count": len(findings),
                    }
                )
            )
        return GuardVerdict(
            allowed=True,
            findings=findings,
            quote_ids=allowed.quote_ids,
            escalation_id=None,
        )

    reason = _escalation_reason(findings)
    escalation_id = _open_escalation(
        conn,
        conversation_id=conversation_id,
        reason=reason,
        quote_ids=allowed.quote_ids,
        findings=findings,
        reply_text=text,
    )
    logger.error(
        json.dumps(
            {
                "event": "output_guard_block",
                "conversation_id": conversation_id,
                "escalation_id": escalation_id,
                "quote_ids": list(allowed.quote_ids),
                "blocked_amount_count": sum(
                    1 for finding in findings if finding.reason != AMOUNT_MATCHED
                ),
                "reasons": [finding.reason for finding in findings],
            }
        )
    )
    return GuardVerdict(
        allowed=False,
        findings=findings,
        quote_ids=allowed.quote_ids,
        escalation_id=escalation_id,
    )
