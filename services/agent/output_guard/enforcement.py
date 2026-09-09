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
    AMOUNT_MATCHED,
    AMOUNT_NOT_IN_QUOTES,
    AmountFinding,
    amounts_are_allowed,
    evaluate_amounts,
)
from services.agent.output_guard.quotes import load_allowed_amounts

logger = logging.getLogger(__name__)

# escalations.reason is free text (migration 0024's own comment names
# "an output-guard violation" as one of exactly two anticipated values) —
# these two distinguish the cases a human must act on differently: a
# malformed amount is likely the model mangling a real number, while a
# well-formed but wrong one is a concrete wrong price.
REASON_UNPARSEABLE = "output_guard_violation_unparseable"
REASON_MISMATCH = "output_guard_violation_mismatch"

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

    A concrete wrong amount (not_in_quotes / below_floor) outranks a
    merely malformed one whenever a reply contains both, since "the agent
    stated a specific wrong price" is the more urgent read for a human
    regardless of what else is wrong with the same reply. The full
    per-amount detail survives either way in notes["reasons"].
    """
    if any(finding.reason in _MISMATCH_REASONS for finding in findings):
        return REASON_MISMATCH
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
