"""services/agent/llm/quote_display.py: the fields a complete quote reply
copies. Pure functions, tested without a database; load_quote_listing is
covered by tests/integration/test_llm_dispatch_integration.py."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from services.agent.fixed_texts import Language
from services.agent.llm.quote_display import (
    QuoteListing,
    distance_displays,
    listing_fields,
    night_count_display,
    night_price_fields,
    room_display,
    stay_fields,
)
from services.agent.output_guard.extraction import extract_candidate_amounts
from services.pricing.compute import NightPrice, Quote

_CHECK_IN = date(2026, 10, 5)


def _quote(asks: list[int]) -> Quote:
    nights = [
        NightPrice(
            stay_date=_CHECK_IN + timedelta(days=index),
            season_id=1,
            ask=ask,
            min_allowed=ask,
            override_applied=True,
        )
        for index, ask in enumerate(asks)
    ]
    return Quote(
        id=1,
        hotel_id=1,
        room_type_id=1,
        check_in=_CHECK_IN,
        check_out=_CHECK_IN + timedelta(days=len(asks)),
        rooms=1,
        ask_price_total=sum(asks),
        min_allowed_total=sum(asks),
        nights=nights,
        negotiation_open=True,
    )


@pytest.mark.parametrize(
    ("meters", "english", "arabic"),
    [
        (0, "0 m", "0 متر"),
        (350, "350 m", "350 متر"),
        (999, "999 m", "999 متر"),
        (1000, "1 km", "1 كم"),
        (1049, "1 km", "1 كم"),
        (1050, "1.1 km", "1.1 كم"),
        (1249, "1.2 km", "1.2 كم"),
        (12_345, "12.3 km", "12.3 كم"),
    ],
)
def test_distance_displays(meters: int, english: str, arabic: str) -> None:
    assert distance_displays(meters) == (english, arabic)


def test_distance_displays_is_none_when_unknown() -> None:
    assert distance_displays(None) == (None, None)


def test_no_distance_rendering_is_ever_a_candidate_amount() -> None:
    """The guard treats thousands-grouped digits as money; a distance must
    never look like one, or a correct quote reply would be blocked. Checked
    across the whole realistic range, alone and inside the approved
    templates' last two lines (prompt.py's quote_reply examples)."""
    for meters in range(0, 30_001, 7):
        english, arabic = distance_displays(meters)
        assert english is not None and arabic is not None
        for text in (english, arabic):
            assert extract_candidate_amounts(text) == (), text
        for text in (
            f"Total *900.00 SAR* (450.00 SAR per night).\n"
            f"Only {english} from the Haram.",
            f"Total *900.00 SAR* (450.00 SAR per malam).\n"
            f"Hanya {english} dari Masjidil Haram.",
            f"الإجمالي *900.00 ريال* (450.00 ريال لليلة).\nيبعد {arabic} عن الحرم.",
        ):
            amounts = [c.halalas for c in extract_candidate_amounts(text)]
            assert amounts == [90_000, 45_000], text


def test_the_lead_in_word_keeps_a_distance_from_being_read_as_money() -> None:
    """Why the English and Indonesian distance lines open with "Only" /
    "Hanya" (owner decision 2026-09-30): the guard reads a digit run as
    money when only punctuation or a line break separates it from a
    currency word, so a bare "350 m" line straight after the total would
    be read as 350.00 SAR. The word in between prevents that even if the
    model drops the per-night text."""
    (_, misread) = extract_candidate_amounts(
        "Total *900.00 SAR*.\n350 m from the Haram."
    )
    assert misread.halalas == 35_000
    for text in (
        "Total *900.00 SAR*.\nOnly 350 m from the Haram.",
        "Total *900.00 SAR*.\nHanya 350 m dari Masjidil Haram.",
    ):
        amounts = [c.halalas for c in extract_candidate_amounts(text)]
        assert amounts == [90_000], text


def test_night_price_fields_gives_one_price_when_every_night_is_the_same() -> None:
    fields = night_price_fields(_quote([45_000, 45_000]))

    assert fields["price_per_night_display"] == "450.00 SAR"
    assert fields["price_per_night_display_ar"] == "450.00 ريال"
    assert fields["lowest_night_price_display"] is None
    assert fields["highest_night_price_display"] is None


def test_night_price_fields_gives_the_lowest_and_highest_real_night_prices() -> None:
    """Never an average: both values are nights the guard already allows."""
    fields = night_price_fields(_quote([40_000, 50_000, 45_000]))

    assert fields["price_per_night_display"] is None
    assert fields["price_per_night_display_ar"] is None
    assert fields["lowest_night_price_display"] == "400.00 SAR"
    assert fields["lowest_night_price_display_ar"] == "400.00 ريال"
    assert fields["highest_night_price_display"] == "500.00 SAR"
    assert fields["highest_night_price_display_ar"] == "500.00 ريال"


def test_night_price_fields_always_has_the_same_keys() -> None:
    assert (
        night_price_fields(_quote([1])).keys()
        == night_price_fields(_quote([1, 2])).keys()
    )


def test_listing_fields() -> None:
    listing = QuoteListing(
        hotel_name="Test Hotel",
        room_type_name="Standard",
        city="madinah",
        distance_to_haram_meters=None,
    )

    assert listing_fields(listing) == {
        "hotel_name": "Test Hotel",
        "room_type_name": "Standard",
        "city": "madinah",
        "distance_to_haram_display": None,
        "distance_to_haram_display_ar": None,
    }


@pytest.mark.parametrize(
    ("nights", "language", "expected"),
    [
        (1, "ar", "ليلة"),
        (2, "ar", "ليلتين"),
        (3, "ar", "3 ليالٍ"),
        (10, "ar", "10 ليالٍ"),
        (11, "ar", "11 ليلة"),
        (14, "ar", "14 ليلة"),
        (1, "en", "1 night"),
        (2, "en", "2 nights"),
        (11, "en", "11 nights"),
        (1, "id", "1 malam"),
        (2, "id", "2 malam"),
    ],
)
def test_night_count_display(nights: int, language: Language, expected: str) -> None:
    """Owner decision 2026-10-01: 1 ليلة، 2 ليلتين، 3-10 ليالٍ، 11+ ليلة;
    night/nights; Indonesian "malam" never changes."""
    assert night_count_display(nights, language) == expected


@pytest.mark.parametrize(
    ("name", "rooms", "language", "expected"),
    [
        ("Standard", 1, "ar", "غرفة Standard"),
        ("Standard", 2, "ar", "غرفتين Standard"),
        ("Standard", 3, "ar", "3 غرف Standard"),
        ("Standard", 11, "ar", "11 غرفة Standard"),
        ("Standard", 1, "en", "Standard room"),
        ("Standard", 2, "en", "2 Standard rooms"),
        ("Standard", 1, "id", "kamar Standard"),
        ("Standard", 2, "id", "2 kamar Standard"),
        ("جناح ملكي", 1, "ar", "جناح ملكي"),
        ("جناح ملكي", 1, "en", "جناح ملكي"),
        ("غرفة مزدوجة", 1, "ar", "غرفة مزدوجة"),
        ("غُرفة مزدوجة", 1, "ar", "غُرفة مزدوجة"),
        ("Deluxe Suite", 1, "en", "Deluxe Suite"),
        ("Standard Room", 1, "ar", "Standard Room"),
        ("Kamar Deluxe", 1, "id", "Kamar Deluxe"),
        ("Suite Royal", 1, "id", "Suite Royal"),
    ],
)
def test_room_display(name: str, rooms: int, language: Language, expected: str) -> None:
    """Owner decision 2026-10-01: no «غرفة» before a name that already
    starts with غرفة or جناح -- and the same for room, suite and kamar."""
    assert room_display(name, rooms, language) == expected


def test_a_room_kind_name_for_several_rooms_gives_the_count_after_it() -> None:
    multiplication_sign = chr(0x00D7)
    assert room_display("جناح ملكي", 2, "ar") == f"جناح ملكي {multiplication_sign} 2"
    assert room_display("Deluxe Suite", 3, "en") == (
        f"Deluxe Suite {multiplication_sign} 3"
    )


def test_stay_fields_render_both_for_every_reply_language() -> None:
    listing = QuoteListing(
        hotel_name="Test Hotel",
        room_type_name="جناح ملكي",
        city="makkah",
        distance_to_haram_meters=16,
    )

    assert stay_fields(_quote([34_385, 34_385]), listing) == {
        "night_count_display": "2 nights",
        "night_count_display_ar": "ليلتين",
        "night_count_display_indonesian": "2 malam",
        "room_display": "جناح ملكي",
        "room_display_ar": "جناح ملكي",
        "room_display_indonesian": "جناح ملكي",
    }


def test_no_night_or_room_rendering_is_ever_a_candidate_amount() -> None:
    texts = [
        night_count_display(n, lang)
        for n in range(1, 31)
        for lang in ("ar", "en", "id")
    ]
    texts += [
        room_display("Standard", n, lang)
        for n in range(1, 31)
        for lang in ("ar", "en", "id")
    ]
    for text in texts:
        assert not extract_candidate_amounts(text), text
