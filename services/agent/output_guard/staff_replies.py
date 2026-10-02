"""The output guard's staff-reply mode (owner decisions 2026-10-01 and
2026-10-02, ARCHITECTURE.md §7 "إبلاغ الموظفين بالتصعيدات", step 3).

A reply a staff member writes from the dashboard goes through the guard
like every other outbound text (CLAUDE.md rule 8), but the guard blocks
none of it: the price check and the booking-claim check do not apply to a
human who holds the conversation. What the guard does instead is find
every amount the reply states, with the same extraction it uses on the
model's replies, so the caller can write them to audit_log before the
reply is sent.

webhook.send_staff_reply_or_log_failure takes a StaffReplyInspection, not
text: the only text that path can send is text inspected here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from services.agent.output_guard.config import MAX_AUDITED_BARE_AMOUNT_DIGITS
from services.agent.output_guard.extraction import (
    CandidateAmount,
    extract_bare_price_echo_candidates,
    extract_candidate_amounts,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StaffReplyInspection:
    """A staff reply's text and every amount it states, in the order the
    extraction found them: marked or money-shaped amounts first, then bare
    ones."""

    text: str
    stated_amounts: tuple[CandidateAmount, ...]


def _stated_amounts(text: str) -> tuple[CandidateAmount, ...]:
    """Every marked or money-shaped amount, and every bare one large enough
    to be a price but short enough not to be a phone or ID number. Broader
    than the model's check on purpose: an audit should over-record, and a
    staff member writing "500" without "ريال" still stated a price."""
    bare = tuple(
        candidate
        for candidate in extract_bare_price_echo_candidates(text)
        if len(candidate.raw) <= MAX_AUDITED_BARE_AMOUNT_DIGITS
    )
    return extract_candidate_amounts(text) + bare


def inspect_staff_reply(text: str, *, conversation_id: int) -> StaffReplyInspection:
    """Runs the guard's staff-reply mode on text: never blocks, returns the
    amounts it states. Logs the amount count only, never the text."""
    inspection = StaffReplyInspection(text=text, stated_amounts=_stated_amounts(text))
    logger.info(
        json.dumps(
            {
                "event": "output_guard_staff_reply",
                "conversation_id": conversation_id,
                "stated_amount_count": len(inspection.stated_amounts),
            }
        )
    )
    return inspection


def audit_record(amount: CandidateAmount) -> dict[str, Any]:
    """One stated amount as audit_log stores it: the digits as written, the
    value in halalas (None when the digits could not be read as one, or for
    a percentage), and what marked it."""
    return {
        "raw": amount.raw,
        "halalas": amount.halalas,
        "sar_marker": amount.has_currency_marker,
        "foreign_currency_marker": amount.foreign_currency_marker,
        "percentage": amount.is_percentage,
    }
