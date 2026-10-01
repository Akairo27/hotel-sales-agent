"""The confirmation sent when a booking yes is answered in code (a tapped
yes button or a bare typed yes -- services/agent/booking_yes.py): the
owner-approved "booking passed on" text (2026-09-30, the same wording
prompt.py shows the model), filled by code from the quote that was
passed on.

Not a fixed_texts.FixedText: it carries the stay's dates and total, so it
has digits, and it is checked by the output guard against the quote like
any reply. The total is lib.money's display of the quote's own
ask_price_total, so a still-valid quote always lets it through.

Dates are the day and the Gregorian month name in the reply's language,
with the year only when check-in and check-out fall in different years.
"""

from __future__ import annotations

from datetime import date

from lib.money import format_halalas_as_arabic_riyal, format_halalas_as_sar
from services.agent.fixed_texts import Language
from services.agent.llm.booking_follow_up import QuoteSummary

_MONTHS: dict[Language, tuple[str, ...]] = {
    "ar": (
        "يناير",
        "فبراير",
        "مارس",
        "أبريل",
        "مايو",
        "يونيو",
        "يوليو",
        "أغسطس",
        "سبتمبر",
        "أكتوبر",
        "نوفمبر",
        "ديسمبر",
    ),
    "en": (
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ),
    "id": (
        "Januari",
        "Februari",
        "Maret",
        "April",
        "Mei",
        "Juni",
        "Juli",
        "Agustus",
        "September",
        "Oktober",
        "November",
        "Desember",
    ),
}

# Arabic counts rooms in three forms: a dual for two, a plural for three
# to ten, and the singular again from eleven.
_ARABIC_DUAL_COUNT = 2
_ARABIC_PLURAL_MAX_COUNT = 10


def format_day_month(day: date, language: Language, *, with_year: bool) -> str:
    """ "5 أكتوبر" / "5 October" / "5 Oktober", plus " 2026" when
    with_year."""
    text = f"{day.day} {_MONTHS[language][day.month - 1]}"
    return f"{text} {day.year}" if with_year else text


def _arabic_rooms(rooms: int, room_type: str) -> str:
    if rooms == 1:
        return f"غرفة {room_type}"
    if rooms == _ARABIC_DUAL_COUNT:
        return f"غرفتين {room_type}"
    if rooms <= _ARABIC_PLURAL_MAX_COUNT:
        return f"{rooms} غرف {room_type}"
    return f"{rooms} غرفة {room_type}"


def _english_rooms(rooms: int, room_type: str) -> str:
    if rooms == 1:
        return f"{room_type} room"
    return f"{rooms} {room_type} rooms"


def _indonesian_rooms(rooms: int, room_type: str) -> str:
    if rooms == 1:
        return f"kamar {room_type}"
    return f"{rooms} kamar {room_type}"


def render_booking_passed_on(quote: QuoteSummary, language: Language) -> str:
    """The approved confirmation for quote, in `language`."""
    with_year = quote.check_in.year != quote.check_out.year
    check_in = format_day_month(quote.check_in, language, with_year=with_year)
    check_out = format_day_month(quote.check_out, language, with_year=with_year)
    if language == "ar":
        rooms = _arabic_rooms(quote.rooms, quote.room_type_name)
        total = format_halalas_as_arabic_riyal(quote.total_halalas)
        return (
            f"أبشر، بلّغت زميلي بطلبك: {quote.hotel_name}، {rooms}، "
            f"من {check_in} إلى {check_out}، الإجمالي {total}. "
            "يتواصل معك قريباً إن شاء الله لتأكيد الحجز."
        )
    total = format_halalas_as_sar(quote.total_halalas)
    if language == "en":
        rooms = _english_rooms(quote.rooms, quote.room_type_name)
        return (
            f"Done — I've passed your request to a colleague: {quote.hotel_name}, "
            f"{rooms}, {check_in} to {check_out}, total {total}. They'll "
            "contact you shortly to confirm the booking."
        )
    rooms = _indonesian_rooms(quote.rooms, quote.room_type_name)
    return (
        f"Baik, permintaan Anda sudah saya teruskan ke rekan saya: "
        f"{quote.hotel_name}, {rooms}, {check_in} sampai {check_out}, total "
        f"{total}. Rekan saya akan segera menghubungi Anda untuk konfirmasi "
        "pemesanan."
    )
