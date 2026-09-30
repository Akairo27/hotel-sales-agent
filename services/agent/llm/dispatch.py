"""Executes a model tool call against the real inventory and pricing code
— CLAUDE.md rules 1 and 2.

This is where those two rules are actually enforced, not just documented:
- Rule 1 (the model never computes a price): every price handed back is
  already final and already formatted by lib/money.py. The model receives
  a string like "1,250.00 SAR", never a bare integer it could do
  arithmetic on.
- Rule 2 (cost never enters the LLM context): compute_quote returns Quote/
  NightPrice objects that carry cost_per_night, target_margin_bps,
  min_profit_halalas, and the rest of the audit trail recorded in
  `quotes` (services/pricing/compute.py). quote_to_tool_result below never
  reads any of those attributes — it builds the outgoing dict key by key,
  by hand, so a field added to NightPrice later cannot ride along into
  the model's context just by existing on the dataclass.

dispatch_tool also logs one agent_tool_call event per call (CLAUDE.md §8:
"log every price decision with its quote_id for traceability"), success or
failure, before returning or re-raising — see _log_tool_call. Its
result_summary is a second, separate hand-built whitelist per tool
(CHECK_AVAILABILITY_LOG_SUMMARY_KEYS / GET_QUOTE_LOG_SUMMARY_KEYS), never a
spread of the already-cost-free tool result: rule 2 says cost must never
reach "a log the model can read" either, and a whitelist that only has to
be correct once (quote_to_tool_result's) is not a whitelist a second
consumer (this log) is protected by for free.

Arguments come from the model, which can hallucinate types, omit
required fields, or send a date range that fails the underlying
services' own validation — all of that is InvalidToolArgumentsError, an
expected failure mode of a function-calling model, not a bug here. This
module still raises it (and logs it with its ToolErrorCode); the loop in
conversation.py turns it into tool_error_result's fixed message for the
model, so a bad argument never ends the turn on its own. A
missing allotment for the requested dates is likewise reported back as
an unpriced result rather than raised, so the model can tell the
customer rather than the whole turn failing — dispatch_get_quote checks
inventory coverage itself (services.inventory.operations.
stay_availability), before ever calling compute_quote, rather
than relying on compute_quote's internal AllotmentNotFoundError: that
error is only raised partway through pricing a night, after price-rule
resolution already ran, so a date range with no allotment *and* no
price rule configured surfaced IncompletePriceRuleChainError instead —
the wrong one of the two, and unhandled. The except AllotmentNotFoundError
below stays as a backstop for the narrow TOCTOU window between this
check and compute_quote's own read. Every other pricing exception (a
price_rules misconfiguration — IncompletePriceRuleChainError,
InconsistentPriceConfigurationError — or NoMatchingBandError, a value
outside its band domain that valid data never produces) is a
business-data problem, not a customer-facing outcome, and is
deliberately left to propagate: the caller crashing the turn (into
webhook.py's no-silence funnel, which escalates it) is more honest than
inventing a way to paper over it here.

get_quote also refuses to price a stay the inventory cannot cover (added
2026-09-28, after a reply quoted a price with no check that any room was
free): dispatch_get_quote calls services.inventory.operations.
stay_availability -- the same read-time test the check_availability tool
exposes -- once, before compute_quote, and returns an unpriced result
when any night cannot supply the rooms requested: "no_allotment_for_dates"
when a night has no inventory row at all (not open for booking yet),
otherwise "insufficient_availability". Argument and date validation come
first of all (a check_in in the past is always an InvalidToolArgumentsError,
whatever the inventory holds), so no inventory decision can ever stand in
for a date problem. compute_quote itself never looks at
free rooms: it only turns (reserved + held) / total into a demand
multiplier, and prices a sold-out night (occupancy 1.0) at the top
occupancy band (until 2026-09-29 that value fell outside every band and
raised NoMatchingBandError, which the gate was masking). The
gate declines a sold-out night with an override too: an
override sets the price, not whether a room exists to sell. The check is
advisory, a read and not a lock: the hold/booking path and the
inventory_never_oversold constraint remain the only real guarantee
against overselling. A last room taken between this read and
compute_quote's own per-night occupancy reads -- a race whose window
grows with the number of nights -- therefore gets a normal price; a quote
is not a sale, and create_hold refuses the stay if no room is left.

Both tools name the nights that block a stay, dates only, in two lists
the model must tell apart (owner decision, 2026-09-29):
"unavailable_nights" (inventory exists, too few free rooms) and
"nights_without_allotment" (no inventory row: not open for booking yet,
never to be called fully booked -- services/agent/staff_follow_up.py opens
a staff escalation for these once per turn). Neither carries a room count
(an owner decision: counts stay hidden from the model -- a result-shape
decision, not a secrecy guarantee, since the `rooms` argument lets
repeated calls probe a threshold).

search_hotels (added 2026-09-28, after an incident where the model
guessed a hotel_id that did not exist) is the id-resolution tool this
module was missing: dispatch_tool now also tracks, per turn, every
(hotel_id, room_type_id) pair search_hotels has actually returned
(`resolved_stays`, threaded in by conversation.py, mutated in place — not
persisted, since D2 in the plan this shipped from scopes enforcement to
one turn only), and dispatch_check_availability/dispatch_get_quote both
reject any pair that is not in it. This is enforcement, not just a
prompt instruction: the model can be told not to guess, but only this
check actually stops it from succeeding anyway, including a pair a
customer states directly as a number.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

import psycopg

from lib.money import format_halalas_as_arabic_riyal, format_halalas_as_sar
from services.agent.hotel_profile import is_hotel_profile_complete
from services.agent.llm.booking_follow_up import (
    REQUEST_BOOKING_FOLLOW_UP_TOOL,
    request_booking_follow_up,
)
from services.agent.llm.errors import (
    InvalidToolArgumentsError,
    ToolErrorCode,
    UnknownToolError,
)
from services.agent.llm.quote_display import (
    QuoteListing,
    listing_fields,
    load_quote_listing,
    night_price_fields,
)
from services.agent.llm.tools import TOOL_ERROR_MESSAGES
from services.inventory.operations import StayAvailability, stay_availability
from services.pricing.compute import Quote, compute_quote
from services.pricing.errors import AllotmentNotFoundError

logger = logging.getLogger(__name__)

SEARCH_HOTELS_TOOL = "search_hotels"
CHECK_AVAILABILITY_TOOL = "check_availability"
GET_QUOTE_TOOL = "get_quote"

# A resolved (hotel_id, room_type_id) pair, as search_hotels actually
# returned it — the unit dispatch_tool's turn-scoped guard tracks.
ResolvedStay = tuple[int, int]

# Every reason get_quote may report for declining to price a stay. A closed
# set, checked by mypy at every _unpriced_result call site -- the only
# producer of an unpriced result -- because the reason is also logged
# (GET_QUOTE_LOG_SUMMARY_KEYS below), which only forwards what that
# function produced. A type-level guarantee, not a runtime filter: the
# journal never carries free text because no call site can pass any.
UnpricedReason = Literal["no_allotment_for_dates", "insufficient_availability"]

# A hotel a customer could plausibly be shown at all: bounds a query
# against a pathological filter (or lack of one) matching most of the
# table, and gives the model something concrete to tell the customer
# ("narrow your search") instead of a silently truncated list.
MAX_SEARCH_HOTELS_RESULTS = 10

# The exact key set a logged tool call's result_summary may ever contain,
# per tool — hand-built the same way QUOTE_RESULT_KEYS is, and for the same
# reason: a field added to Quote/NightPrice later must not ride along into
# the journal just by existing on the dataclass. Deliberately narrower than
# QUOTE_RESULT_KEYS itself (no total_price_display, no nights) — this log
# has no need to duplicate the customer-facing price, only quote_id, which
# CLAUDE.md rule 8 asks to log for traceability.
SEARCH_HOTELS_LOG_SUMMARY_KEYS = frozenset({"result_count", "truncated"})
CHECK_AVAILABILITY_LOG_SUMMARY_KEYS = frozenset({"available"})
# reason is None for a priced result and one of UnpricedReason's literals
# otherwise -- so an unpriced call is traceable to its cause in the journal
# (no_allotment_for_dates and insufficient_availability look identical
# without it).
GET_QUOTE_LOG_SUMMARY_KEYS = frozenset({"priced", "quote_id", "reason"})

# search_hotels' own result whitelist, the same hand-built discipline as
# QUOTE_RESULT_KEYS: no cost column exists on hotels or room_types at all,
# but the fields below are still built one by one rather than as a row
# spread, so a column added to either table later cannot reach the model
# just by existing.
SEARCH_HOTELS_RESULT_KEYS = frozenset({"hotels", "truncated"})
HOTEL_RESULT_KEYS = frozenset(
    {
        "hotel_id",
        "hotel_name",
        "city",
        "zone",
        "district_name",
        "star_rating",
        "distance_to_haram_meters",
        "room_types",
    }
)
ROOM_TYPE_RESULT_KEYS = frozenset(
    {"room_type_id", "room_type_name", "capacity_adults", "bed_configuration"}
)

# Arabic name-matching normalization: collapses spelling variants a
# customer's own typing is likely to produce (alef with/without hamza,
# taa marbuta vs. haa, alef maksura vs. yaa) and strips tashkeel
# diacritics, applied to both hotel_name and the customer's search term,
# at query time, in the database — see _SEARCH_HOTELS_QUERY's own
# translate() calls. Plain translate(), not a new Postgres extension
# (neither unaccent nor pg_trgm is installed on this project): every
# mapping here is exactly one character to at most one character, passed
# in as the bound %(norm_from)s/%(norm_to)s parameters, never
# interpolated into the query text.
#
# Built via chr(), not string literals: a literal Arabic character here
# is exactly what RUF001 (ambiguous-unicode-character) exists to flag on
# an isolated single-letter string (this file's ordinary Arabic prose
# elsewhere is long enough that ruff never flags it), and, unlike an
# escape sequence, ruff's own formatter cannot silently rewrite a chr()
# call back into a raw glyph. Named by their Unicode character name, not
# transliterated, so each mapping is checkable against the Unicode
# standard directly.
_ALEF_HAMZA_ABOVE = chr(0x0623)  # ARABIC LETTER ALEF WITH HAMZA ABOVE
_ALEF_HAMZA_BELOW = chr(0x0625)  # ARABIC LETTER ALEF WITH HAMZA BELOW
_ALEF_MADDA_ABOVE = chr(0x0622)  # ARABIC LETTER ALEF WITH MADDA ABOVE
_ALEF_WASLA = chr(0x0671)  # ARABIC LETTER ALEF WASLA
_BARE_ALEF = chr(0x0627)  # ARABIC LETTER ALEF
_TAA_MARBUTA = chr(0x0629)  # ARABIC LETTER TEH MARBUTA
_HAA = chr(0x0647)  # ARABIC LETTER HEH
_ALEF_MAKSURA = chr(0x0649)  # ARABIC LETTER ALEF MAKSURA
_YAA = chr(0x064A)  # ARABIC LETTER YEH
_TASHKEEL = (
    chr(0x064B)  # ARABIC FATHATAN
    + chr(0x064C)  # ARABIC DAMMATAN
    + chr(0x064D)  # ARABIC KASRATAN
    + chr(0x064E)  # ARABIC FATHA
    + chr(0x064F)  # ARABIC DAMMA
    + chr(0x0650)  # ARABIC KASRA
    + chr(0x0651)  # ARABIC SHADDA
    + chr(0x0652)  # ARABIC SUKUN
    + chr(0x0670)  # ARABIC LETTER SUPERSCRIPT ALEF
)  # deleted, not mapped

_ALEF_VARIANTS = _ALEF_HAMZA_ABOVE + _ALEF_HAMZA_BELOW + _ALEF_MADDA_ABOVE + _ALEF_WASLA
_NORMALIZE_FROM = _ALEF_VARIANTS + _TAA_MARBUTA + _ALEF_MAKSURA + _TASHKEEL
# Shorter than _NORMALIZE_FROM on purpose: translate() deletes any
# trailing `from` characters with no corresponding `to` character, which
# is exactly what _TASHKEEL above needs (removed, not replaced).
_NORMALIZE_TO = (_BARE_ALEF * len(_ALEF_VARIANTS)) + _HAA + _YAA


# The exact key set quote_to_tool_result may ever produce — the
# enforcement point tests/unit/test_llm_dispatch.py checks rule 2
# against.
QUOTE_RESULT_KEYS = frozenset(
    {
        "priced",
        "quote_id",
        "hotel_id",
        "room_type_id",
        "check_in",
        "check_out",
        "rooms",
        "total_price_display",
        "total_price_display_ar",
        "nights",
        "negotiation_open",
        # quote_display.py: what a complete quote reply needs, copied only.
        "hotel_name",
        "room_type_name",
        "city",
        "night_count",
        "price_per_night_display",
        "price_per_night_display_ar",
        "lowest_night_price_display",
        "lowest_night_price_display_ar",
        "highest_night_price_display",
        "highest_night_price_display_ar",
        "distance_to_haram_display",
        "distance_to_haram_display_ar",
    }
)
UNPRICED_RESULT_KEYS = frozenset(
    {
        "priced",
        "reason",
        "hotel_id",
        "room_type_id",
        "check_in",
        "check_out",
        "unavailable_nights",
        "nights_without_allotment",
    }
)
NIGHT_RESULT_KEYS = frozenset({"date", "price_display", "price_display_ar"})
TOOL_ERROR_RESULT_KEYS = frozenset({"error", "message"})


@dataclass(frozen=True)
class StayArgs:
    """Parsed, validated arguments shared by both tools' schemas."""

    hotel_id: int
    room_type_id: int
    check_in: date
    check_out: date
    rooms: int


def _require_int(args: dict[str, Any], key: str) -> int:
    value = args.get(key)
    if isinstance(value, bool):
        raise InvalidToolArgumentsError(f"{key} must be an integer, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise InvalidToolArgumentsError(f"{key} must be an integer, got {value!r}")


def _require_date(args: dict[str, Any], key: str) -> date:
    value = args.get(key)
    if not isinstance(value, str):
        raise InvalidToolArgumentsError(f"{key} must be a string, got {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidToolArgumentsError(
            f"{key}={value!r} is not a valid ISO 8601 date"
        ) from exc


def parse_stay_args(args: dict[str, Any]) -> StayArgs:
    """Validates the shared (hotel_id, room_type_id, check_in, check_out,
    rooms) shape both tool schemas declare.

    Raises:
        InvalidToolArgumentsError: a field is missing, the wrong type, or
            the parsed values fail a basic sanity check (check_out not
            after check_in, rooms not positive).
    """
    stay = StayArgs(
        hotel_id=_require_int(args, "hotel_id"),
        room_type_id=_require_int(args, "room_type_id"),
        check_in=_require_date(args, "check_in"),
        check_out=_require_date(args, "check_out"),
        rooms=_require_int(args, "rooms"),
    )
    if stay.check_out <= stay.check_in:
        raise InvalidToolArgumentsError("check_out must be after check_in")
    if stay.rooms <= 0:
        raise InvalidToolArgumentsError("rooms must be positive")
    return stay


_VALID_CITIES = frozenset({"makkah", "madinah"})
_VALID_ZONES = frozenset(
    {
        "makkah_central",
        "makkah_outside",
        "madinah_central",
        "madinah_north",
        "madinah_west",
        "madinah_south",
        "madinah_outside",
    }
)


@dataclass(frozen=True)
class SearchHotelsArgs:
    """Parsed, validated search_hotels arguments — every field optional
    except that at least one must be present (parse_search_hotels_args)."""

    hotel_name: str | None
    city: str | None
    zone: str | None
    min_star_rating: int | None
    max_star_rating: int | None


def _require_optional_str_in(
    args: dict[str, Any], key: str, valid: frozenset[str]
) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or value not in valid:
        raise InvalidToolArgumentsError(
            f"{key} must be one of {sorted(valid)}, got {value!r}"
        )
    return value


def _require_optional_star_rating(args: dict[str, Any], key: str) -> int | None:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidToolArgumentsError(f"{key} must be an integer, got {value!r}")
    if not 1 <= value <= 5:
        raise InvalidToolArgumentsError(f"{key} must be between 1 and 5, got {value!r}")
    return value


def parse_search_hotels_args(args: dict[str, Any]) -> SearchHotelsArgs:
    """Validates search_hotels' arguments.

    Raises:
        InvalidToolArgumentsError: a field has the wrong type or an
            invalid value, no field at all was given, or min_star_rating
            exceeds max_star_rating.
    """
    hotel_name = args.get("hotel_name")
    if hotel_name is not None and not isinstance(hotel_name, str):
        raise InvalidToolArgumentsError(
            f"hotel_name must be a string, got {hotel_name!r}"
        )
    search = SearchHotelsArgs(
        hotel_name=hotel_name,
        city=_require_optional_str_in(args, "city", _VALID_CITIES),
        zone=_require_optional_str_in(args, "zone", _VALID_ZONES),
        min_star_rating=_require_optional_star_rating(args, "min_star_rating"),
        max_star_rating=_require_optional_star_rating(args, "max_star_rating"),
    )
    if all(
        value is None
        for value in (
            search.hotel_name,
            search.city,
            search.zone,
            search.min_star_rating,
            search.max_star_rating,
        )
    ):
        raise InvalidToolArgumentsError("search_hotels requires at least one filter")
    if (
        search.min_star_rating is not None
        and search.max_star_rating is not None
        and search.min_star_rating > search.max_star_rating
    ):
        raise InvalidToolArgumentsError(
            "min_star_rating must not exceed max_star_rating"
        )
    return search


def _room_types_by_hotel_id(
    conn: psycopg.Connection[Any], hotel_ids: list[int]
) -> dict[int, list[dict[str, Any]]]:
    rows = conn.execute(
        "SELECT hotel_id, id, room_type_name, capacity_adults, bed_configuration "
        "FROM room_types WHERE hotel_id = ANY(%(hotel_ids)s) ORDER BY hotel_id, id",
        {"hotel_ids": hotel_ids},
    ).fetchall()
    by_hotel: dict[int, list[dict[str, Any]]] = {hotel_id: [] for hotel_id in hotel_ids}
    for (
        hotel_id,
        room_type_id,
        room_type_name,
        capacity_adults,
        bed_configuration,
    ) in rows:
        by_hotel[hotel_id].append(
            {
                "room_type_id": room_type_id,
                "room_type_name": room_type_name,
                "capacity_adults": capacity_adults,
                "bed_configuration": bed_configuration,
            }
        )
    return by_hotel


_SEARCH_HOTELS_QUERY = (
    "SELECT id, hotel_name, city, zone, district_name, star_rating, "
    "distance_to_haram_meters, address_text FROM hotels "
    "WHERE is_active "
    "AND ("
    "    %(hotel_name)s::text IS NULL "
    "    OR translate(lower(hotel_name), %(norm_from)s::text, %(norm_to)s::text) LIKE "
    "       '%%' || translate(lower(%(hotel_name)s::text), %(norm_from)s::text, "
    "       %(norm_to)s::text) || '%%'"
    ") "
    "AND (%(city)s::text IS NULL OR city = %(city)s::text) "
    "AND (%(zone)s::text IS NULL OR zone = %(zone)s::text) "
    "AND (%(min_star_rating)s::smallint IS NULL "
    "     OR star_rating >= %(min_star_rating)s::smallint) "
    "AND (%(max_star_rating)s::smallint IS NULL "
    "     OR star_rating <= %(max_star_rating)s::smallint) "
    "ORDER BY id"
)


@dataclass(frozen=True)
class _HotelRow:
    """One row of _SEARCH_HOTELS_QUERY, named so the row-to-result mapping
    below reads as field names, not tuple positions."""

    hotel_id: int
    hotel_name: str
    city: str | None
    zone: str | None
    district_name: str | None
    star_rating: int | None
    distance_to_haram_meters: int | None
    address_text: str | None


def _hotel_result(row: _HotelRow, room_types: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "hotel_id": row.hotel_id,
        "hotel_name": row.hotel_name,
        "city": row.city,
        "zone": row.zone,
        "district_name": row.district_name,
        "star_rating": row.star_rating,
        "distance_to_haram_meters": row.distance_to_haram_meters,
        "room_types": room_types,
    }


def dispatch_search_hotels(
    conn: psycopg.Connection[Any], args: dict[str, Any]
) -> dict[str, Any]:
    """Executes search_hotels: resolves what a customer said about a hotel
    or room type into real ids and structured, cost-free facts. Only
    active hotels with a complete profile
    (services.agent.hotel_profile.is_hotel_profile_complete) are ever
    returned — an incomplete profile gives the model no honest basis to
    describe a hotel, the gate ARCHITECTURE.md §4 asks every
    customer-facing tool to apply. Two or more matching hotels are all
    returned, never narrowed to one — see tools.py's description of this
    tool for why (the incident this tool exists to prevent).
    """
    search = parse_search_hotels_args(args)
    rows = conn.execute(
        _SEARCH_HOTELS_QUERY,
        {
            "hotel_name": search.hotel_name,
            "city": search.city,
            "zone": search.zone,
            "min_star_rating": search.min_star_rating,
            "max_star_rating": search.max_star_rating,
            "norm_from": _NORMALIZE_FROM,
            "norm_to": _NORMALIZE_TO,
        },
    ).fetchall()

    hotel_rows = [_HotelRow(*row) for row in rows]
    complete_hotels = [
        row
        for row in hotel_rows
        if is_hotel_profile_complete(
            distance_to_haram_meters=row.distance_to_haram_meters,
            star_rating=row.star_rating,
            address_text=row.address_text,
            city=row.city,
            zone=row.zone,
        )
    ]
    truncated = len(complete_hotels) > MAX_SEARCH_HOTELS_RESULTS
    kept = complete_hotels[:MAX_SEARCH_HOTELS_RESULTS]

    kept_hotel_ids = [row.hotel_id for row in kept]
    room_types_by_hotel_id = _room_types_by_hotel_id(conn, kept_hotel_ids)
    hotels = [_hotel_result(row, room_types_by_hotel_id[row.hotel_id]) for row in kept]
    return {"hotels": hotels, "truncated": truncated}


def quote_to_tool_result(quote: Quote, listing: QuoteListing) -> dict[str, Any]:
    """Converts a priced Quote into the exact, cost-free dict shape sent
    to the model. See the module docstring for why this exists. Every
    price comes twice, the same number with a different currency word:
    *_display ("1,250.00 SAR") for English and Indonesian replies and
    *_display_ar ("1,250.00 ريال") for Arabic ones (prompt.py's
    price_currency_word) -- so the model copies a finished string in
    either language and never relabels a price itself. listing adds the
    hotel's names and distance (quote_display.py).
    """
    return {
        "priced": True,
        "quote_id": quote.id,
        "hotel_id": quote.hotel_id,
        "room_type_id": quote.room_type_id,
        "check_in": quote.check_in.isoformat(),
        "check_out": quote.check_out.isoformat(),
        "rooms": quote.rooms,
        "total_price_display": format_halalas_as_sar(quote.ask_price_total),
        "total_price_display_ar": format_halalas_as_arabic_riyal(quote.ask_price_total),
        "nights": [
            {
                "date": night.stay_date.isoformat(),
                "price_display": format_halalas_as_sar(night.ask),
                "price_display_ar": format_halalas_as_arabic_riyal(night.ask),
            }
            for night in quote.nights
        ],
        "negotiation_open": quote.negotiation_open,
        "night_count": len(quote.nights),
        **night_price_fields(quote),
        **listing_fields(listing),
    }


def _night_list(nights: tuple[date, ...]) -> list[str]:
    return [night.isoformat() for night in nights]


def _availability_fields(availability: StayAvailability) -> dict[str, Any]:
    """The two date lists both tools report -- dates only, never a count
    (see this module's docstring)."""
    return {
        "unavailable_nights": _night_list(availability.unavailable_nights),
        "nights_without_allotment": _night_list(availability.nights_without_allotment),
    }


def _stay_availability_for(
    conn: psycopg.Connection[Any], stay: StayArgs
) -> StayAvailability:
    return stay_availability(
        conn,
        stay.hotel_id,
        stay.room_type_id,
        stay.check_in,
        stay.check_out,
        stay.rooms,
    )


def _unpriced_result(
    stay: StayArgs, *, reason: UnpricedReason, availability: StayAvailability
) -> dict[str, Any]:
    return {
        "priced": False,
        "reason": reason,
        "hotel_id": stay.hotel_id,
        "room_type_id": stay.room_type_id,
        "check_in": stay.check_in.isoformat(),
        "check_out": stay.check_out.isoformat(),
        **_availability_fields(availability),
    }


def dispatch_check_availability(
    conn: psycopg.Connection[Any], args: dict[str, Any], *, now: datetime
) -> dict[str, Any]:
    """Executes check_availability. Never touches cost — this tool never
    returns anything price-related at all.

    A check_in before today is rejected before any inventory read, the
    same as get_quote (owner decision, 2026-09-30): a date without a year
    that has already passed this year must be confirmed with the customer,
    never answered with the availability of a past night.

    Raises:
        InvalidToolArgumentsError: the arguments fail validation, or
            check_in is in the past (code past_check_in).
    """
    stay = parse_stay_args(args)
    _require_check_in_not_past(stay, now)
    availability = _stay_availability_for(conn, stay)
    return {
        "available": availability.is_available,
        "hotel_id": stay.hotel_id,
        "room_type_id": stay.room_type_id,
        "check_in": stay.check_in.isoformat(),
        "check_out": stay.check_out.isoformat(),
        "rooms": stay.rooms,
        **_availability_fields(availability),
    }


def _require_check_in_not_past(stay: StayArgs, now: datetime) -> None:
    """Rejects a check_in before today, ahead of every inventory read.

    The same rule, the same comparison and the same message as
    compute_quote's own validation (which stays as the backstop): without
    this the inventory checks in dispatch_get_quote would decide a
    past-dated stay first, so it would be reported as unpriced -- or, on a
    night with free rooms, as an invalid argument -- depending on
    inventory alone.

    Raises:
        InvalidToolArgumentsError: check_in is earlier than now's date.
    """
    if stay.check_in < now.date():
        raise InvalidToolArgumentsError(
            "check_in must not be in the past", code="past_check_in"
        )


def dispatch_get_quote(
    conn: psycopg.Connection[Any],
    args: dict[str, Any],
    *,
    now: datetime,
    customer_phone: str | None,
    conversation_id: int | None,
) -> dict[str, Any]:
    """Executes get_quote: prices the stay, records it in `quotes`
    (compute_quote's own responsibility), and returns the cost-free
    result the model may relay to the customer.

    The arguments are validated first, including that check_in is not in
    the past, before any inventory is read: a bad date gets the same
    InvalidToolArgumentsError whatever the inventory looks like, never an
    unpriced result that would blame availability for a date problem.

    A valid stay is then declined, unpriced and with no `quotes` row
    written, when a night has no inventory row ("no_allotment_for_dates")
    or, failing that, when any night has fewer free rooms than requested
    ("insufficient_availability"). Either way the result lists the nights
    by reason (unavailable_nights, nights_without_allotment). The check is
    a read, not a lock -- see this module's docstring for why it is
    advisory and what it deliberately does not reveal.

    Raises:
        InvalidToolArgumentsError: the arguments fail validation
            (parse_stay_args, or a check_in in the past), or compute_quote
            rejects them.
        StayListingNotFoundError: the hotel or room type row is gone
            (quote_display.load_quote_listing), read before pricing so no
            quote is written for it.
        Any other services.pricing exception compute_quote raises for a
            price_rules misconfiguration -- deliberately left to propagate
            (see this module's docstring).
    """
    stay = parse_stay_args(args)
    _require_check_in_not_past(stay, now)
    availability = _stay_availability_for(conn, stay)
    if availability.nights_without_allotment:
        return _unpriced_result(
            stay, reason="no_allotment_for_dates", availability=availability
        )
    if availability.unavailable_nights:
        return _unpriced_result(
            stay, reason="insufficient_availability", availability=availability
        )
    listing = load_quote_listing(conn, stay.hotel_id, stay.room_type_id)
    try:
        quote = compute_quote(
            conn,
            stay.hotel_id,
            stay.room_type_id,
            stay.check_in,
            stay.check_out,
            stay.rooms,
            now,
            customer_phone=customer_phone,
            conversation_id=conversation_id,
        )
    except ValueError as exc:
        raise InvalidToolArgumentsError(str(exc)) from exc
    except AllotmentNotFoundError:
        # The allotment vanished after the read above: read again, so the
        # result names the nights that are actually missing now.
        return _unpriced_result(
            stay,
            reason="no_allotment_for_dates",
            availability=_stay_availability_for(conn, stay),
        )

    return quote_to_tool_result(quote, listing)


def _search_hotels_log_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {"result_count": len(result["hotels"]), "truncated": result["truncated"]}


def _check_availability_log_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {"available": result["available"]}


def _get_quote_log_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "priced": result["priced"],
        "quote_id": result.get("quote_id"),
        "reason": result.get("reason"),
    }


def _booking_follow_up_log_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "quote_id": result["quote_id"],
        "already_requested": result["already_requested"],
    }


def _dispatch_booking_follow_up(
    conn: psycopg.Connection[Any],
    args: dict[str, Any],
    *,
    conversation_id: int | None,
) -> dict[str, Any]:
    """Validates quote_id and hands over to booking_follow_up.py, which
    owns every check and the write."""
    quote_id = _require_int(args, "quote_id")
    if conversation_id is None:
        raise InvalidToolArgumentsError(
            "request_booking_follow_up needs a conversation",
            code="quote_not_confirmable",
        )
    return request_booking_follow_up(
        conn, quote_id=quote_id, conversation_id=conversation_id
    )


def _require_resolved_stay(stay: StayArgs, resolved_stays: set[ResolvedStay]) -> None:
    """The turn-scoped enforcement half of the search_hotels fix (the
    module docstring's other half): rejects a hotel_id/room_type_id pair
    search_hotels did not actually return earlier in this same turn,
    whether the model invented it or a customer stated it directly.
    """
    if (stay.hotel_id, stay.room_type_id) not in resolved_stays:
        raise InvalidToolArgumentsError(
            f"hotel_id={stay.hotel_id}, room_type_id={stay.room_type_id} was not "
            "returned by search_hotels in this conversation turn",
            code="unresolved_stay",
        )


def tool_error_result(code: ToolErrorCode) -> dict[str, Any]:
    """The result handed back to the model in place of a rejected tool
    call's output -- built only from the closed ToolErrorCode set and its
    fixed message (tools.TOOL_ERROR_MESSAGES), never from the exception's
    own text or the call's arguments. Key set is TOOL_ERROR_RESULT_KEYS."""
    return {"error": code, "message": TOOL_ERROR_MESSAGES[code]}


def _log_tool_call(
    *,
    conversation_id: int | None,
    tool_name: str,
    args: dict[str, Any],
    result_summary: dict[str, Any] | None,
    error_type: str | None,
    error_code: ToolErrorCode | None = None,
) -> None:
    """The one place a model tool call is logged — CLAUDE.md rule 8. Never
    logs cost or any other floor-related pricing field: `args` is the
    tool's own input (verified free of those by every declared tool's
    schema in tools.py), and `result_summary` is always one of the
    hand-built per-tool summaries (the *_LOG_SUMMARY_KEYS sets), never a
    spread of the tool's result dict.
    error_code is set only for an InvalidToolArgumentsError -- one of the
    closed ToolErrorCode literals, so the journal shows which fixed message
    the model was handed without logging the exception's text.
    """
    logger.info(
        json.dumps(
            {
                "event": "agent_tool_call",
                "conversation_id": conversation_id,
                "tool_name": tool_name,
                "arguments": args,
                "result_summary": result_summary,
                "error_type": error_type,
                "error_code": error_code,
            }
        )
    )


def dispatch_tool(
    conn: psycopg.Connection[Any],
    name: str,
    args: dict[str, Any],
    *,
    now: datetime,
    customer_phone: str | None,
    conversation_id: int | None,
    resolved_stays: set[ResolvedStay],
) -> dict[str, Any]:
    """Routes a model tool call by name to its handler, logging exactly one
    agent_tool_call event per call (success or failure) before returning or
    re-raising — see _log_tool_call.

    `resolved_stays` is this turn's own state, owned and threaded in by the
    caller (conversation.py) across every dispatch_tool call in the turn's
    tool-calling loop — never persisted, never read from the database. A
    successful search_hotels call adds every (hotel_id, room_type_id) pair
    it returned; check_availability and get_quote both reject any pair not
    already in it (_require_resolved_stay). request_booking_follow_up
    needs no resolved stay: it acts on a quote this conversation already
    has (booking_follow_up.py).

    Raises:
        UnknownToolError: name is not one of the tools declared in
            tools.py. Never executed silently.
        InvalidToolArgumentsError: see the individual dispatch functions,
            and, for check_availability/get_quote, a hotel_id/room_type_id
            pair not in `resolved_stays`.
    """
    try:
        if name == SEARCH_HOTELS_TOOL:
            result = dispatch_search_hotels(conn, args)
            for hotel in result["hotels"]:
                for room_type in hotel["room_types"]:
                    resolved_stays.add((hotel["hotel_id"], room_type["room_type_id"]))
            _log_tool_call(
                conversation_id=conversation_id,
                tool_name=name,
                args=args,
                result_summary=_search_hotels_log_summary(result),
                error_type=None,
            )
            return result
        if name == CHECK_AVAILABILITY_TOOL:
            _require_resolved_stay(parse_stay_args(args), resolved_stays)
            result = dispatch_check_availability(conn, args, now=now)
            _log_tool_call(
                conversation_id=conversation_id,
                tool_name=name,
                args=args,
                result_summary=_check_availability_log_summary(result),
                error_type=None,
            )
            return result
        if name == GET_QUOTE_TOOL:
            _require_resolved_stay(parse_stay_args(args), resolved_stays)
            result = dispatch_get_quote(
                conn,
                args,
                now=now,
                customer_phone=customer_phone,
                conversation_id=conversation_id,
            )
            _log_tool_call(
                conversation_id=conversation_id,
                tool_name=name,
                args=args,
                result_summary=_get_quote_log_summary(result),
                error_type=None,
            )
            return result
        if name == REQUEST_BOOKING_FOLLOW_UP_TOOL:
            result = _dispatch_booking_follow_up(
                conn, args, conversation_id=conversation_id
            )
            _log_tool_call(
                conversation_id=conversation_id,
                tool_name=name,
                args=args,
                result_summary=_booking_follow_up_log_summary(result),
                error_type=None,
            )
            return result
        raise UnknownToolError(f"model called unknown tool {name!r}")
    except Exception as exc:
        _log_tool_call(
            conversation_id=conversation_id,
            tool_name=name,
            args=args,
            result_summary=None,
            error_type=type(exc).__name__,
            error_code=exc.code if isinstance(exc, InvalidToolArgumentsError) else None,
        )
        raise
