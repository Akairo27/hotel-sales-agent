"""Finds a claim that a booking was passed on, confirmed or made -- the
output guard's second check (owner decision 2026-10-01, ARCHITECTURE.md
§7). Pure text matching, no I/O.

Why: in eval run 36819478043 the model answered a customer's yes with the
approved "I've passed your request to a colleague" text in 8 turns
without calling request_booking_follow_up, so the customer was promised a
call no escalation backed. The confirmation is now always the fixed text
code renders after a successful request (services/agent/
booking_confirmation.py), and the guard blocks any reply holding one of
these phrases unless such a request succeeded in the same turn.

The phrases are claims in the past tense or a passive "is confirmed",
specific to a booking request: the booking offer itself ("Shall I pass
this to a colleague...?", «تحب أبلّغ زميلي...؟», "Mau saya teruskan...?"),
the fixed texts, and the not-yet-open reply («بلّغت زميلنا ويتواصل
معك») contain none of them -- tests/unit/test_output_guard_booking_claims.py
pins that. Matched after services/agent/text_matching.normalize_for_matching,
so diacritics, alef forms, case and spacing do not matter. A paraphrase
outside the list is not caught: the list closes the wording the model was
shown and its closest variants, not every possible sentence.
"""

from __future__ import annotations

from services.agent.text_matching import normalize_for_matching

ARABIC_BOOKING_CLAIMS: tuple[str, ...] = (
    "بلّغت زميلي بطلبك",
    "بلّغت زميلي بطلب الحجز",
    "بلّغت زميلنا بطلبك",
    "بلّغت الزميل بطلبك",
    "حوّلت طلبك",
    "حوّلت طلب الحجز",
    "رفعت طلبك",
    "أرسلت طلبك",
    "تم تحويل طلبك",
    "تم إرسال طلبك",
    "تم رفع طلبك",
    "تم تأكيد الحجز",
    "تم تأكيد حجزك",
    "تأكد حجزك",
    "حجزك مؤكد",
    "أكدت لك الحجز",
    "أكدت حجزك",
    "حجزت لك",
)
ENGLISH_BOOKING_CLAIMS: tuple[str, ...] = (
    "passed your request",
    "passed your booking",
    "passed this to a colleague",
    "passed it to a colleague",
    "forwarded your request",
    "forwarded your booking",
    "sent your request to a colleague",
    "booking is confirmed",
    "booking has been confirmed",
    "reservation is confirmed",
    "reservation has been confirmed",
    "your room is booked",
    "booked the room for you",
    "booked a room for you",
    "booked it for you",
)
INDONESIAN_BOOKING_CLAIMS: tuple[str, ...] = (
    "sudah saya teruskan",
    "telah saya teruskan",
    "sudah diteruskan",
    "telah diteruskan",
    "saya sudah meneruskan",
    "saya telah meneruskan",
    "pemesanan anda sudah dikonfirmasi",
    "pemesanan anda telah dikonfirmasi",
    "pemesanan sudah dikonfirmasi",
    "pemesanan telah dikonfirmasi",
    "sudah saya pesankan",
    "telah saya pesankan",
)

_NORMALIZED_CLAIMS: tuple[str, ...] = tuple(
    normalize_for_matching(phrase)
    for phrase in (
        *ARABIC_BOOKING_CLAIMS,
        *ENGLISH_BOOKING_CLAIMS,
        *INDONESIAN_BOOKING_CLAIMS,
    )
)


def find_booking_claims(text: str) -> tuple[str, ...]:
    """Every listed phrase text contains, in its normalized form (what a
    blocked escalation's notes record -- a fixed phrase, never the
    reply)."""
    normalized = normalize_for_matching(text)
    return tuple(phrase for phrase in _NORMALIZED_CLAIMS if phrase in normalized)
