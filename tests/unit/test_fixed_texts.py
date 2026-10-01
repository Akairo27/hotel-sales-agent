"""services/agent/fixed_texts.py -- the fixed customer texts, their
renderings and the language choice. No database: customer_language's
query is tested in tests/integration/test_fixed_texts.py, and every
rendering's pass through the output guard in
tests/integration/test_output_guard.py.

The digit checks moved here from tests/unit/test_output_guard_enforcement.py
with the fallback text itself (2026-09-30): no rendering may contain a
digit, which is what makes every one provably always allowed by the guard.
"""

from __future__ import annotations

import pytest

from services.agent.fixed_texts import (
    BOOKING_QUESTION_BUTTON,
    BOOKING_YES_BUTTON,
    FALLBACK,
    NEWER_PRICE,
    PLEASE_TYPE,
    FixedText,
    Language,
    detect_language,
    media_placeholder,
)
from services.agent.whatsapp_send import REPLY_BUTTON_TITLE_MAX_CHARS

_LANGUAGES: tuple[Language | None, ...] = ("ar", "en", "id", None)
_FIXED_TEXTS = (
    ("fallback", FALLBACK),
    ("please_type", PLEASE_TYPE),
    ("booking_yes_button", BOOKING_YES_BUTTON),
    ("booking_question_button", BOOKING_QUESTION_BUTTON),
    ("newer_price", NEWER_PRICE),
)
_RENDERINGS = [
    pytest.param(text.render(language), id=f"{name}-{language or 'bilingual'}")
    for name, text in _FIXED_TEXTS
    for language in _LANGUAGES
]
# U+0660-U+0669, the Arabic-Indic digits: checked on their own because an
# editor fixing an ASCII digit they can see has no reason to think of one
# they might not recognize as a digit.
_ARABIC_INDIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"


def test_the_owner_approved_wording_is_pinned() -> None:
    assert FALLBACK.arabic == (
        "لحظة لو سمحت، خلّيني أتأكد من طلبك مع زميلي، ويتواصل معك قريباً إن شاء الله."
    )
    assert FALLBACK.english == (
        "One moment — I need to double-check this with a colleague, and "
        "they'll follow up with you shortly."
    )
    assert FALLBACK.indonesian == (
        "Mohon tunggu sebentar — saya perlu memastikan hal ini dengan rekan "
        "saya, dan dia akan segera menghubungi Anda."
    )
    assert PLEASE_TYPE.arabic == (
        "المعذرة، ما أقدر أسمع الرسائل الصوتية ولا أشوف الصور حالياً. اكتب لي "
        "طلبك وأخدمك مباشرة."
    )
    assert PLEASE_TYPE.english == (
        "Sorry, I can't read voice notes or images yet. Please type your "
        "request and I'll help you right away."
    )
    assert PLEASE_TYPE.indonesian == (
        "Maaf, saya belum bisa membaca pesan suara atau gambar. Silakan "
        "ketik permintaan Anda, dan saya akan langsung membantu."
    )


def test_the_booking_button_and_newer_price_wording_is_pinned() -> None:
    """Owner-approved 2026-10-01."""
    assert (
        BOOKING_YES_BUTTON.arabic,
        BOOKING_YES_BUTTON.english,
        BOOKING_YES_BUTTON.indonesian,
    ) == ("نعم، أكّد الحجز", "Yes, confirm", "Ya, konfirmasi")
    assert (
        BOOKING_QUESTION_BUTTON.arabic,
        BOOKING_QUESTION_BUTTON.english,
        BOOKING_QUESTION_BUTTON.indonesian,
    ) == ("عندي سؤال", "I have a question", "Ada pertanyaan")
    assert NEWER_PRICE.arabic == (
        "في سعر أحدث من هذا بالأعلى. تحب أبلّغ زميلي يؤكّد لك الحجز على السعر الأحدث؟"
    )
    assert NEWER_PRICE.english == (
        "There's a newer price above this one. Shall I pass the newer one to "
        "a colleague to confirm your booking?"
    )
    assert NEWER_PRICE.indonesian == (
        "Ada harga yang lebih baru di atas. Mau saya teruskan yang terbaru ke "
        "rekan saya untuk konfirmasi pemesanan?"
    )


@pytest.mark.parametrize("language", ["ar", "en", "id"])
@pytest.mark.parametrize("text", [BOOKING_YES_BUTTON, BOOKING_QUESTION_BUTTON])
def test_every_button_title_fits_whatsapp(text: FixedText, language: Language) -> None:
    assert 1 <= len(text.render(language)) <= REPLY_BUTTON_TITLE_MAX_CHARS


@pytest.mark.parametrize("rendering", _RENDERINGS)
def test_no_rendering_has_an_ascii_digit(rendering: str) -> None:
    assert not any(ch.isascii() and ch.isdigit() for ch in rendering)


@pytest.mark.parametrize("rendering", _RENDERINGS)
def test_no_rendering_has_an_arabic_indic_digit(rendering: str) -> None:
    assert not any(ch in _ARABIC_INDIC_DIGITS for ch in rendering)


@pytest.mark.parametrize("rendering", _RENDERINGS)
def test_no_rendering_has_a_digit_in_any_script(rendering: str) -> None:
    """str.isdigit() covers every Unicode decimal-digit script -- the same
    definition extraction.py's _DIGIT_RUN relies on."""
    assert not any(ch.isdigit() for ch in rendering)


@pytest.mark.parametrize("text", [text for _, text in _FIXED_TEXTS])
def test_each_known_language_gets_that_language_alone(text: FixedText) -> None:
    assert text.render("ar") == text.arabic
    assert text.render("en") == text.english
    assert text.render("id") == text.indonesian


@pytest.mark.parametrize("text", [FALLBACK, PLEASE_TYPE])
def test_an_unknown_language_gets_arabic_then_english(text: FixedText) -> None:
    assert text.render(None) == f"{text.arabic}\n{text.english}"


@pytest.mark.parametrize(
    ("message", "language"),
    [
        pytest.param("السلام عليكم، أبغى غرفة", "ar", id="arabic"),
        pytest.param("Hello, do you have a room?", "en", id="english"),
        pytest.param("Halo, berapa harga kamar?", "id", id="indonesian"),
        pytest.param("SAYA MAU KAMAR", "id", id="indonesian-upper-case"),
        pytest.param("Terima kasih!", "id", id="indonesian-thanks"),
        pytest.param("Terima   kasih", "id", id="indonesian-thanks-spaced"),
        pytest.param("I need a room at the hotel", "en", id="hotel-is-not-indonesian"),
        pytest.param("Kamarnya bagus", "en", id="whole-words-only"),
        pytest.param("غرفة for 2 nights", "en", id="mostly-latin"),
        pytest.param("أبغى غرفة ok", "ar", id="mostly-arabic"),
        pytest.param("ab غر", "ar", id="a-tie-counts-as-arabic"),
        pytest.param("12/10 - 15/10", None, id="digits-only"),
        pytest.param("", None, id="empty"),
    ],
)
def test_detect_language(message: str, language: Language | None) -> None:
    assert detect_language(message) == language


def test_media_placeholder_format() -> None:
    assert media_placeholder("audio") == "[audio message]"
