"""services/agent/output_guard — the last line of defense before a reply
reaches a customer (ARCHITECTURE.md §7, CLAUDE.md rule 8).

Everything a caller needs is re-exported here, mirroring
services/agent/llm/__init__.py's pattern.
"""

from __future__ import annotations

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

__all__ = [
    "AMOUNT_BELOW_FLOOR",
    "AMOUNT_MATCHED",
    "AMOUNT_NOT_IN_QUOTES",
    "AMOUNT_UNPARSEABLE",
    "REASON_MISMATCH",
    "REASON_UNPARSEABLE",
    "AllowedAmounts",
    "AmountFinding",
    "CandidateAmount",
    "GuardVerdict",
    "amounts_are_allowed",
    "enforce_outbound_text",
    "evaluate_amounts",
    "extract_candidate_amounts",
    "load_allowed_amounts",
    "normalize_for_scanning",
    "parse_amount_to_halalas",
]
