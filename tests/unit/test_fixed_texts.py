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
    FALLBACK,
    PLEASE_TYPE,
    FixedText,
    Language,
    detect_language,
    media_placeholder,
)

_LANGUAGES: tuple[Language | None, ...] = ("ar", "en", "id", None)
_RENDERINGS = [
    pytest.param(text.render(language), id=f"{name}-{language or 'bilingual'}")
    for name, text in (("fallback", FALLBACK), ("please_type", PLEASE_TYPE))
    for language in _LANGUAGES
]
# U+0660-U+0669, the Arabic-Indic digits: checked on their own because an
# editor fixing an ASCII digit they can see has no reason to think of one
# they might not recognize as a digit.
_ARABIC_INDIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"


def test_the_owner_approved_wording_is_pinned() -> None:
    assert FALLBACK.arabic == (
        "لحظة لو سمحت، بتأكد من هالموضوع مع زميلي وبيتواصل معك قريب إن شاء الله."
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
        "المعذرة، ما أقدر أسمع الرسائل الصوتية ولا أشوف الصور للحين. اكتب لي "
        "طلبك وأخدمك على طول."
    )
    assert PLEASE_TYPE.english == (
        "Sorry, I can't read voice notes or images yet. Please type your "
        "request and I'll help you right away."
    )
    assert PLEASE_TYPE.indonesian == (
        "Maaf, saya belum bisa membaca pesan suara atau gambar. Silakan "
        "ketik permintaan Anda, dan saya akan langsung membantu."
    )


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


@pytest.mark.parametrize("text", [FALLBACK, PLEASE_TYPE])
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
