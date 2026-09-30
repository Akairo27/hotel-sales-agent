"""The fields get_quote adds to a priced result so the model can write a
complete quote reply by copying alone (owner decision 2026-09-30,
ARCHITECTURE.md §7): the hotel and room type names, the hotel's city, the
number of nights, one price per night (or the lowest and highest when the
nights differ), and the distance to the city's reference mosque.

Every price here is one of the quote's own nightly prices, picked, never
computed: the output guard allows a quote's total and each night's price
and nothing else (services/agent/output_guard/quotes.py), and CLAUDE.md
rule 1 keeps price arithmetic out of the model. The distance is formatted
here too, and never with a thousands separator: the guard treats "1,200"
as money (services/agent/output_guard/extraction.py), so a reply saying
"1,200 m" would be blocked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

from lib.money import format_halalas_as_arabic_riyal, format_halalas_as_sar
from services.agent.llm.errors import StayListingNotFoundError
from services.pricing.compute import Quote

METERS_PER_KILOMETER = 1000
# Kilometres are shown to one decimal place, rounded half up.
_METERS_PER_TENTH_KILOMETER = 100
_TENTHS_PER_KILOMETER = 10


@dataclass(frozen=True)
class QuoteListing:
    """What a quote reply says about the hotel, read from hotels and
    room_types -- columns migration 0030 grants hotel_agent."""

    hotel_name: str
    room_type_name: str
    city: str | None
    distance_to_haram_meters: int | None


def load_quote_listing(
    conn: psycopg.Connection[Any], hotel_id: int, room_type_id: int
) -> QuoteListing:
    """Reads the hotel's and room type's names, city and distance.

    Raises:
        StayListingNotFoundError: no room type room_type_id belongs to a
            hotel hotel_id.
    """
    row = conn.execute(
        "SELECT h.hotel_name, rt.room_type_name, h.city, h.distance_to_haram_meters "
        "FROM room_types AS rt JOIN hotels AS h ON h.id = rt.hotel_id "
        "WHERE h.id = %s AND rt.id = %s",
        (hotel_id, room_type_id),
    ).fetchone()
    if row is None:
        raise StayListingNotFoundError(
            f"no room type {room_type_id} in hotel {hotel_id}"
        )
    return QuoteListing(
        hotel_name=row[0],
        room_type_name=row[1],
        city=row[2],
        distance_to_haram_meters=row[3],
    )


def distance_displays(meters: int | None) -> tuple[str | None, str | None]:
    """The distance for an English or Indonesian reply and for an Arabic
    one: "350 m" / "350 متر" under a kilometre, otherwise kilometres to one
    decimal ("1.2 km" / "1.2 كم", "1 km" when the decimal is 0). (None,
    None) when the distance is unknown."""
    if meters is None:
        return None, None
    if meters < METERS_PER_KILOMETER:
        return f"{meters} m", f"{meters} متر"
    tenths = (meters + _METERS_PER_TENTH_KILOMETER // 2) // _METERS_PER_TENTH_KILOMETER
    whole, tenth = divmod(tenths, _TENTHS_PER_KILOMETER)
    kilometers = f"{whole}" if tenth == 0 else f"{whole}.{tenth}"
    return f"{kilometers} km", f"{kilometers} كم"


def night_price_fields(quote: Quote) -> dict[str, str | None]:
    """One price per room per night when every night costs the same;
    otherwise the lowest and the highest night's price. Both are real
    night prices, never an average. The fields that do not apply are
    None, so the result always has the same keys."""
    asks = [night.ask for night in quote.nights]
    lowest, highest = min(asks), max(asks)
    uniform = lowest == highest
    return {
        "price_per_night_display": format_halalas_as_sar(lowest) if uniform else None,
        "price_per_night_display_ar": (
            format_halalas_as_arabic_riyal(lowest) if uniform else None
        ),
        "lowest_night_price_display": None
        if uniform
        else format_halalas_as_sar(lowest),
        "lowest_night_price_display_ar": (
            None if uniform else format_halalas_as_arabic_riyal(lowest)
        ),
        "highest_night_price_display": (
            None if uniform else format_halalas_as_sar(highest)
        ),
        "highest_night_price_display_ar": (
            None if uniform else format_halalas_as_arabic_riyal(highest)
        ),
    }


def listing_fields(listing: QuoteListing) -> dict[str, str | None]:
    """The hotel's names, city and formatted distance for the result."""
    distance, distance_ar = distance_displays(listing.distance_to_haram_meters)
    return {
        "hotel_name": listing.hotel_name,
        "room_type_name": listing.room_type_name,
        "city": listing.city,
        "distance_to_haram_display": distance,
        "distance_to_haram_display_ar": distance_ar,
    }
