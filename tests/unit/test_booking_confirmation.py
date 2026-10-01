"""services/agent/booking_confirmation.py: the approved "booking passed
on" text, filled by code. Its pass through the output guard is in
tests/integration/test_booking_yes.py."""

from __future__ import annotations

from datetime import date

import pytest

from services.agent.booking_confirmation import (
    format_day_month,
    render_booking_passed_on,
)
from services.agent.fixed_texts import Language
from services.agent.llm.booking_follow_up import QuoteSummary

_QUOTE = QuoteSummary(
    quote_id=1,
    hotel_name="Test Hotel",
    room_type_name="Standard",
    check_in=date(2026, 10, 20),
    check_out=date(2026, 10, 22),
    rooms=1,
    total_halalas=40_000,
)


def _with_rooms(rooms: int) -> QuoteSummary:
    return QuoteSummary(
        quote_id=_QUOTE.quote_id,
        hotel_name=_QUOTE.hotel_name,
        room_type_name=_QUOTE.room_type_name,
        check_in=_QUOTE.check_in,
        check_out=_QUOTE.check_out,
        rooms=rooms,
        total_halalas=_QUOTE.total_halalas,
    )


def test_the_arabic_confirmation_is_the_approved_text() -> None:
    assert render_booking_passed_on(_QUOTE, "ar") == (
        "أبشر، بلّغت زميلي بطلبك: Test Hotel، غرفة Standard، من 20 أكتوبر إلى "
        "22 أكتوبر، الإجمالي 400.00 ريال. يتواصل معك قريباً إن شاء الله لتأكيد "
        "الحجز."
    )


def test_the_english_confirmation_is_the_approved_text() -> None:
    assert render_booking_passed_on(_QUOTE, "en") == (
        "Done — I've passed your request to a colleague: Test Hotel, Standard "
        "room, 20 October to 22 October, total 400.00 SAR. They'll contact you "
        "shortly to confirm the booking."
    )


def test_the_indonesian_confirmation_is_the_approved_text() -> None:
    assert render_booking_passed_on(_QUOTE, "id") == (
        "Baik, permintaan Anda sudah saya teruskan ke rekan saya: Test Hotel, "
        "kamar Standard, 20 Oktober sampai 22 Oktober, total 400.00 SAR. Rekan "
        "saya akan segera menghubungi Anda untuk konfirmasi pemesanan."
    )


@pytest.mark.parametrize(
    ("rooms", "language", "expected"),
    [
        (1, "ar", "غرفة Standard"),
        (2, "ar", "غرفتين Standard"),
        (3, "ar", "3 غرف Standard"),
        (10, "ar", "10 غرف Standard"),
        (11, "ar", "11 غرفة Standard"),
        (1, "en", "Standard room"),
        (2, "en", "2 Standard rooms"),
        (1, "id", "kamar Standard"),
        (2, "id", "2 kamar Standard"),
    ],
)
def test_the_room_count_reads_naturally(
    rooms: int, language: Language, expected: str
) -> None:
    comma = "،" if language == "ar" else ","
    rendered = render_booking_passed_on(_with_rooms(rooms), language)

    assert f"{comma} {expected}{comma} " in rendered


@pytest.mark.parametrize(
    ("language", "expected"),
    [("ar", "5 مايو"), ("en", "5 May"), ("id", "5 Mei")],
)
def test_a_date_is_the_day_and_the_month_name(
    language: Language, expected: str
) -> None:
    assert format_day_month(date(2027, 5, 5), language, with_year=False) == expected
    assert (
        format_day_month(date(2027, 5, 5), language, with_year=True)
        == f"{expected} 2027"
    )


@pytest.mark.parametrize("language", ["ar", "en", "id"])
def test_every_month_has_its_own_name(language: Language) -> None:
    names = {
        format_day_month(date(2027, month, 1), language, with_year=False)
        for month in range(1, 13)
    }
    assert len(names) == 12


def test_the_year_is_shown_only_when_the_stay_crosses_into_a_new_year() -> None:
    new_year = QuoteSummary(
        quote_id=1,
        hotel_name="Test Hotel",
        room_type_name="Standard",
        check_in=date(2026, 12, 30),
        check_out=date(2027, 1, 2),
        rooms=1,
        total_halalas=40_000,
    )

    assert "30 December 2026 to 2 January 2027" in render_booking_passed_on(
        new_year, "en"
    )
    assert "2026" not in render_booking_passed_on(_QUOTE, "en")
