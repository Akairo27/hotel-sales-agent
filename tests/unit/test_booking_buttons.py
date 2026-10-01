"""services/agent/booking_buttons.py: the button ids, when a reply gets
buttons, what the guard is shown, and the bare-yes matcher (owner
decisions 2026-10-01). No database: the checks against it are in
tests/integration/test_booking_yes.py."""

from __future__ import annotations

from datetime import date

import pytest

from lib.hijri import to_hijri
from services.agent.booking_buttons import (
    BARE_YES_WORDS,
    BookingOfferButtons,
    ParsedButtonId,
    _normalized,
    booking_offer_buttons,
    button_id,
    buttons_for_reply,
    ends_with_booking_offer,
    is_bare_decisive_yes,
    offer_language,
    parse_button_id,
    text_with_button_titles,
)
from services.agent.fixed_texts import (
    BOOKING_QUESTION_BUTTON,
    BOOKING_YES_BUTTON,
    NEWER_PRICE,
    Language,
)
from services.agent.llm.prompt import render_system_instruction
from services.agent.whatsapp_send import (
    REPLY_BUTTONS_BODY_MAX_CHARS,
    ReplyButton,
    check_reply_buttons,
)

# The booking offer that ends each of prompt.py's quote reply examples.
_ARABIC_OFFER = "تحب أبلّغ زميلي يؤكّد لك الحجز؟"
_ENGLISH_OFFER = "Shall I pass this to a colleague to confirm your booking?"
_INDONESIAN_OFFER = "Mau saya teruskan ke rekan saya untuk konfirmasi pemesanan?"
_OFFERS: tuple[tuple[str, Language], ...] = (
    (_ARABIC_OFFER, "ar"),
    (_ENGLISH_OFFER, "en"),
    (_INDONESIAN_OFFER, "id"),
)
_QUOTE_REPLY = (
    "Test Hotel, Standard room, 2 nights, 20 to 22 October:\n"
    "Total *400.00 SAR* (200.00 SAR per night).\n"
    "Only 350 m from the Haram.\n" + _ENGLISH_OFFER
)


def test_a_button_id_round_trips() -> None:
    assert parse_button_id(button_id("yes", 42)) == ParsedButtonId("yes", 42)
    assert parse_button_id(button_id("question", 7)) == ParsedButtonId("question", 7)
    assert button_id("yes", 42) == "booking:yes:42"


@pytest.mark.parametrize(
    "raw_id",
    [
        "booking:yes:",
        "booking:yes:abc",
        "booking:maybe:1",
        "booking:yes:1:2",
        " booking:yes:1",
        "booking:yes:1\n",
        "booking:yes:" + "1" * 19,
        "booking:yes:" + chr(0x0661),  # ARABIC-INDIC DIGIT ONE
        "BOOKING:YES:1",
        "",
    ],
)
def test_anything_but_a_button_id_this_module_made_is_refused(raw_id: str) -> None:
    assert parse_button_id(raw_id) is None


@pytest.mark.parametrize(
    ("language", "yes_title", "question_title"),
    [
        ("ar", "نعم، أكّد الحجز", "عندي سؤال"),
        ("en", "Yes, confirm", "I have a question"),
        ("id", "Ya, konfirmasi", "Ada pertanyaan"),
    ],
)
def test_the_offer_buttons_are_titled_in_the_offer_s_language(
    language: Language, yes_title: str, question_title: str
) -> None:
    offer = booking_offer_buttons(9, language)

    assert offer == BookingOfferButtons(
        quote_id=9,
        buttons=(
            ReplyButton(button_id="booking:yes:9", title=yes_title),
            ReplyButton(button_id="booking:question:9", title=question_title),
        ),
    )
    check_reply_buttons("x" * REPLY_BUTTONS_BODY_MAX_CHARS, offer.buttons)


@pytest.mark.parametrize(("offer", "language"), _OFFERS)
def test_the_prompt_s_own_offers_are_recognised(offer: str, language: str) -> None:
    """Pinned to the prompt itself: rewording an offer there without
    updating booking_buttons._OFFER_MARKERS would silently stop the
    buttons."""
    prompt = render_system_instruction(
        customer_name=None,
        today=date(2026, 10, 1),
        today_hijri=to_hijri(date(2026, 10, 1)),
        current_stay=None,
    )

    assert offer in prompt
    assert ends_with_booking_offer(f"Test Hotel, total 400.00 SAR.\n{offer}")
    assert offer_language(offer) == language


@pytest.mark.parametrize("language", ["ar", "en", "id"])
def test_the_newer_price_text_is_itself_an_offer(language: Language) -> None:
    assert ends_with_booking_offer(NEWER_PRICE.render(language))


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="empty"),
        pytest.param(_ENGLISH_OFFER.removesuffix("?"), id="no-question-mark"),
        pytest.param(f"{_ENGLISH_OFFER}\nThanks!", id="offer-not-last"),
        pytest.param(f"{_ENGLISH_OFFER} 😊", id="something-after-the-question"),
        pytest.param("Shall I ask a colleague?", id="no-booking-marker"),
        pytest.param("Shall I confirm your booking?", id="no-colleague-marker"),
        pytest.param("تحب أشوف لك تواريخ ثانية؟", id="another-arabic-question"),
    ],
)
def test_anything_else_is_not_an_offer(text: str) -> None:
    assert not ends_with_booking_offer(text)


def test_an_offer_with_diacritics_or_trailing_blank_lines_is_recognised() -> None:
    assert ends_with_booking_offer("تُحب أبلّغ زَميلي يؤكّد لك الحَجز؟\n\n  ")


def test_a_reply_offering_one_priced_stay_gets_buttons() -> None:
    assert buttons_for_reply(_QUOTE_REPLY, (5,)) == booking_offer_buttons(5, "en")


def test_the_buttons_follow_the_reply_s_language() -> None:
    reply = f"Hotel Uji, kamar Standard, 2 malam:\n{_INDONESIAN_OFFER}"

    offer = buttons_for_reply(reply, (5,))

    assert offer is not None
    assert offer.buttons[0].title == BOOKING_YES_BUTTON.indonesian
    assert offer.buttons[1].title == BOOKING_QUESTION_BUTTON.indonesian


@pytest.mark.parametrize(
    ("text", "quote_ids"),
    [
        pytest.param(_QUOTE_REPLY, (), id="nothing-priced"),
        pytest.param(_QUOTE_REPLY, (5, 6), id="two-stays-priced"),
        pytest.param("Test Hotel, total 400.00 SAR.", (5,), id="no-offer"),
        pytest.param(
            "x" * REPLY_BUTTONS_BODY_MAX_CHARS + "\n" + _ENGLISH_OFFER,
            (5,),
            id="over-the-body-limit",
        ),
    ],
)
def test_a_reply_that_does_not_qualify_goes_as_plain_text(
    text: str, quote_ids: tuple[int, ...]
) -> None:
    assert buttons_for_reply(text, quote_ids) is None


def test_a_reply_at_exactly_the_body_limit_still_gets_buttons() -> None:
    reply = "x" * (REPLY_BUTTONS_BODY_MAX_CHARS - len(_ENGLISH_OFFER) - 1)
    reply = f"{reply}\n{_ENGLISH_OFFER}"
    assert len(reply) == REPLY_BUTTONS_BODY_MAX_CHARS

    assert buttons_for_reply(reply, (5,)) is not None


def test_an_offer_with_no_letters_to_detect_defaults_to_arabic() -> None:
    assert offer_language("?") == "ar"


def test_the_guard_sees_the_body_then_every_title() -> None:
    offer = booking_offer_buttons(5, "ar")

    assert text_with_button_titles("body", offer) == (
        "body\n\nنعم، أكّد الحجز\nعندي سؤال"
    )
    assert text_with_button_titles("body", None) == "body"


def test_every_bare_yes_word_is_listed_in_its_normalized_form() -> None:
    assert all(_normalized(word) == word for word in BARE_YES_WORDS)


@pytest.mark.parametrize("word", sorted(BARE_YES_WORDS))
def test_each_listed_word_alone_is_a_bare_yes(word: str) -> None:
    assert is_bare_decisive_yes(word)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("إيه", id="alef-hamza-below"),
        pytest.param("أيوه", id="alef-hamza-above"),
        pytest.param("أكيد", id="akeed-with-hamza"),
        pytest.param("إحجز", id="ihjiz-with-hamza"),
        pytest.param("نَعَم", id="diacritics"),
        pytest.param("تـــم", id="tatweel"),
        pytest.param("  نعم  ", id="surrounding-space"),
        pytest.param("Ok", id="capitalised"),
        pytest.param("YES", id="upper-case"),
        # FULLWIDTH LATIN CAPITAL LETTER O, K
        pytest.param(chr(0xFF2F) + chr(0xFF2B), id="full-width"),
        pytest.param("ok.", id="trailing-full-stop"),
        pytest.param("yes!!", id="trailing-exclamations"),
        pytest.param("iya!", id="indonesian-exclaimed"),
        pytest.param("\U0001f44d", id="thumbs-up"),
        pytest.param("\U0001f44d\U0001f3fd", id="thumbs-up-skin-tone"),
        pytest.param("\U0001f44d️", id="thumbs-up-variation-selector"),
    ],
)
def test_spelling_variants_of_a_bare_yes_are_accepted(text: str) -> None:
    assert is_bare_decisive_yes(text)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("تمام", id="tamam-alone"),
        pytest.param("تمام شكراً", id="tamam-thanks"),
        pytest.param("نعم شكرا", id="yes-thanks"),
        pytest.param("يعطيك العافية", id="courtesy"),
        pytest.param("تسلم", id="exit-word"),
        pytest.param("ما قصرت", id="exit-phrase"),
        pytest.param("thanks", id="thanks"),
        pytest.param("ok thanks", id="ok-thanks"),
        pytest.param("yes please", id="yes-please"),
        pytest.param("ايه؟", id="arabic-question-mark"),
        pytest.param("ok?", id="question-mark"),
        pytest.param("ok,", id="comma"),
        pytest.param("ايه بس 3 ليالي", id="yes-with-a-change"),
        pytest.param("نعم 2", id="digit"),
        pytest.param("3", id="digit-alone"),
        pytest.param("\U0001f44d\U0001f44d", id="two-thumbs"),
        pytest.param("\U0001f44c", id="ok-hand"),
        pytest.param("\U0001f64f", id="folded-hands"),
        pytest.param("لا", id="no-arabic"),
        pytest.param("no", id="no"),
        pytest.param("y", id="single-letter"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
    ],
)
def test_anything_else_is_left_to_the_model(text: str) -> None:
    assert not is_bare_decisive_yes(text)
