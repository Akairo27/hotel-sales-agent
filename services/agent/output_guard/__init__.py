"""services/agent/output_guard — the last line of defense before a reply
reaches a customer (ARCHITECTURE.md §7, CLAUDE.md rule 8).

Everything a caller needs is re-exported here, mirroring
services/agent/llm/__init__.py's pattern.
"""

from __future__ import annotations

from services.agent.output_guard.booking_claims import find_booking_claims
from services.agent.output_guard.decision import (
    AMOUNT_BELOW_FLOOR,
    AMOUNT_MATCHED,
    AMOUNT_NOT_IN_QUOTES,
    AMOUNT_UNPARSEABLE,
    AllowedAmounts,
    AmountFinding,
    amounts_are_allowed,
    evaluate_amounts,
)
from services.agent.output_guard.enforcement import (
    REASON_BOOKING_CLAIM,
    REASON_MISMATCH,
    REASON_UNPARSEABLE,
    GuardVerdict,
    enforce_outbound_text,
)
from services.agent.output_guard.extraction import (
    CandidateAmount,
    extract_candidate_amounts,
    normalize_for_scanning,
    parse_amount_to_halalas,
)
from services.agent.output_guard.quotes import load_allowed_amounts
from services.agent.output_guard.staff_replies import (
    StaffReplyInspection,
    audit_record,
    inspect_staff_reply,
)

__all__ = [
    "AMOUNT_BELOW_FLOOR",
    "AMOUNT_MATCHED",
    "AMOUNT_NOT_IN_QUOTES",
    "AMOUNT_UNPARSEABLE",
    "REASON_BOOKING_CLAIM",
    "REASON_MISMATCH",
    "REASON_UNPARSEABLE",
    "AllowedAmounts",
    "AmountFinding",
    "CandidateAmount",
    "GuardVerdict",
    "StaffReplyInspection",
    "amounts_are_allowed",
    "audit_record",
    "enforce_outbound_text",
    "evaluate_amounts",
    "extract_candidate_amounts",
    "find_booking_claims",
    "inspect_staff_reply",
    "load_allowed_amounts",
    "normalize_for_scanning",
    "parse_amount_to_halalas",
]
