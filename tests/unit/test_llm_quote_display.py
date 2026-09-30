"""services/agent/llm/quote_display.py: the fields a complete quote reply
copies. Pure functions, tested without a database; load_quote_listing is
covered by tests/integration/test_llm_dispatch_integration.py."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from services.agent.llm.quote_display import (
    QuoteListing,
    distance_displays,
    listing_fields,
    night_price_fields,
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
