"""The one normalization for matching a customer's or the model's words
against a fixed list: the bare-yes matcher and the booking-offer marker
(services/agent/booking_buttons.py), and the output guard's
booking-claim phrases (services/agent/output_guard/booking_claims.py).

Arabic spelling variants are collapsed with the same letter mapping
search_hotels applies in the database (services/agent/llm/dispatch.py),
plus tatweel, so a phrase matches however it is decorated.
"""

from __future__ import annotations

import unicodedata

from services.agent.llm.dispatch import ARABIC_NORMALIZE_FROM, ARABIC_NORMALIZE_TO

_TATWEEL = chr(0x0640)  # ARABIC TATWEEL
_KEPT_LETTER_COUNT = len(ARABIC_NORMALIZE_TO)
_ARABIC_LETTER_MAPPING = str.maketrans(
    ARABIC_NORMALIZE_FROM[:_KEPT_LETTER_COUNT],
    ARABIC_NORMALIZE_TO,
    ARABIC_NORMALIZE_FROM[_KEPT_LETTER_COUNT:] + _TATWEEL,
)


def normalize_for_matching(text: str) -> str:
    """NFKC, lower case, Arabic spelling variants collapsed (tatweel and
    diacritics removed, alef forms unified, taa marbuta and alef maksura
    mapped as search_hotels maps them), and every run of whitespace made
    one space, trimmed."""
    folded = unicodedata.normalize("NFKC", text).lower()
    return " ".join(folded.translate(_ARABIC_LETTER_MAPPING).split())
