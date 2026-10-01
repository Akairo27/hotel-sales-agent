"""services/agent/output_guard/booking_claims.py: what counts as a claim
that a booking was passed on, confirmed or made (owner decision
2026-10-01), and what never does. Its use by enforce_outbound_text is in
tests/integration/test_output_guard.py."""

from __future__ import annotations

from datetime import date

import pytest

from services.agent.booking_confirmation import render_booking_passed_on
from services.agent.fixed_texts import (
    BOOKING_QUESTION_BUTTON,
    BOOKING_YES_BUTTON,
    FALLBACK,
    NEWER_PRICE,
    PLEASE_TYPE,
    Language,
)
from services.agent.llm import prompt as prompt_module
from services.agent.llm.booking_follow_up import QuoteSummary
from services.agent.output_guard.booking_claims import (
    ARABIC_BOOKING_CLAIMS,
    ENGLISH_BOOKING_CLAIMS,
    INDONESIAN_BOOKING_CLAIMS,
    find_booking_claims,
)

_ALL_PHRASES = (
    *ARABIC_BOOKING_CLAIMS,
    *ENGLISH_BOOKING_CLAIMS,
    *INDONESIAN_BOOKING_CLAIMS,
)
_QUOTE = QuoteSummary(
    quote_id=1,
    hotel_name="Test Hotel",
    room_type_name="Standard",
    check_in=date(2026, 10, 5),
    check_out=date(2026, 10, 7),
    rooms=1,
    total_halalas=90_000,
)

# Replies the model wrote in eval run 36819478043 without calling
# request_booking_follow_up (synthetic data), and the question-tap reply
# that claimed a booking.
_FALSE_CONFIRMATIONS = (
    "أبشر، بلّغت زميلي بطلبك: Test Hotel، غرفة Standard، من 5 إلى 7 أكتوبر "
    "2026، غرفة وحدة، الإجمالي *900.00 ريال*. يتواصل معك قريباً إن شاء الله "
    "لتأكيد الحجز.",
    "Done — I've passed your request to a colleague: Test Hotel, Standard "
    "room, 5 to 7 October, total 900.00 SAR. They'll contact you shortly to "
    "confirm the booking.",
    "Baik, permintaan Anda sudah saya teruskan ke rekan saya: Test Hotel, "
    "kamar Standard, 5 sampai 7 Oktober, total *900.00 SAR*. Rekan saya akan "
    "segera menghubungi Anda untuk konfirmasi pemesanan.",
    "أبشر، بلّغت زميلي بطلبك: Test Hotel، غرفة Standard، من 5 إلى 7 أكتوبر "
    "2026، الإجمالي 900.00 ريال. يتواصل معك قريباً إن شاء الله لتأكيد الحجز."
    "\n\nتفضل، إيش سؤالك؟",
)


@pytest.mark.parametrize("phrase", _ALL_PHRASES)
def test_every_listed_phrase_is_a_claim_inside_a_sentence(phrase: str) -> None:
    assert find_booking_claims(f"Okay. {phrase} -- thanks!")


@pytest.mark.parametrize("reply", _FALSE_CONFIRMATIONS)
def test_the_false_confirmations_the_eval_found_are_claims(reply: str) -> None:
    assert find_booking_claims(reply)


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param("ابشر، بلغت زميلي بطلبك.", id="no-diacritics"),
        pytest.param("بلّغـــت زميلي   بطلبك", id="tatweel-and-spaces"),
        pytest.param("تم تاكيد الحجز", id="bare-alef"),
        pytest.param("I've PASSED YOUR REQUEST to a colleague.", id="upper-case"),
        pytest.param("Your booking has been confirmed.", id="passive"),
        pytest.param("Pemesanan Anda sudah dikonfirmasi.", id="indonesian-passive"),
    ],
)
def test_a_claim_is_found_however_it_is_spelled(reply: str) -> None:
    assert find_booking_claims(reply)


@pytest.mark.parametrize("language", ["ar", "en", "id"])
def test_the_code_rendered_confirmation_is_itself_a_claim(language: Language) -> None:
    """Which is why the guard lets it through only when the booking was
    really passed on in that turn."""
    assert find_booking_claims(render_booking_passed_on(_QUOTE, language))


def _legitimate_texts() -> list[str]:
    fixed = [
        text.render(language)
        for text in (
            FALLBACK,
            PLEASE_TYPE,
            NEWER_PRICE,
            BOOKING_YES_BUTTON,
            BOOKING_QUESTION_BUTTON,
        )
        for language in ("ar", "en", "id", None)
    ]
    return [
        *fixed,
        *prompt_module.CUSTOMER_FACING_ARABIC_EXAMPLES,
        prompt_module._ENGLISH_EXAMPLE_QUOTE_REPLY,
        prompt_module._INDONESIAN_EXAMPLE_QUOTE_REPLY,
        "Sorry, that room is fully booked on 21 October.",
        "للأسف الغرف كلها محجوزة ليلة 21 أكتوبر.",
        "Shall I pass this to a colleague to confirm your booking?",
        "Mau saya teruskan ke rekan saya untuk konfirmasi pemesanan?",
        "Sure, go ahead and ask — I'm happy to help!",
    ]


@pytest.mark.parametrize("text", _legitimate_texts())
def test_offers_fixed_texts_and_the_prompt_s_examples_are_never_claims(
    text: str,
) -> None:
    """The booking offer, every fixed text, every example the prompt
    shows, the not-yet-open reply («بلّغت زميلنا ويتواصل معك») and a
    sold-out reply."""
    assert find_booking_claims(text) == ()
