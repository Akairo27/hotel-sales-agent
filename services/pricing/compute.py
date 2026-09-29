"""compute_quote — ARCHITECTURE.md §5's full pricing computation sequence.

Per night: resolve the season, then a manual override if one is active
(skipping margin/demand/floor entirely), otherwise resolve the inherited
price rule, look up cost, and compute ask_price (cost + margin, scaled by
the demand factor) and min_allowed (cost + a lead-time-banded minimum
profit — demand never touches this number). Every quote is recorded to
the append-only quotes table before being returned.

Pure in spirit, not in the letter — CLAUDE.md's "no I/O" rule for
pricing functions is about not hiding a clock read or a hidden business
formula inside the calculation; this function's I/O is exactly the
database reads/write the calculation is defined in terms of. `now` is
still always passed in, never read from the clock internally.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast

import psycopg
from psycopg.types.json import Json

from services.inventory.hold_windows import LAST_MINUTE_THRESHOLD
from services.pricing.bands import lookup_band_value
from services.pricing.demand import compute_demand_factor, compute_occupancy
from services.pricing.errors import (
    AllotmentNotFoundError,
    InconsistentPriceConfigurationError,
)
from services.pricing.rules import resolve_price_rule
from services.pricing.seasons import resolve_season_id

_BPS_SCALE = 10_000


@dataclass(frozen=True)
class NightPrice:
    """One night's per-room price, as recorded in quotes.nights — with
    enough of the computation kept that "why was this priced like this"
    is answerable from the record months later, not from guessing.

    The detail fields are None only when override_applied is True: an
    active manual override skips margin/demand/floor entirely, so there
    is no computation to report — see db/migrations/0009_quotes_nights_audit.sql
    for the DB-enforced version of this same either/or shape.
    """

    stay_date: date
    season_id: int
    ask: int
    min_allowed: int
    override_applied: bool
    cost_per_night: int | None = None
    occupancy: float | None = None
    target_margin_bps: int | None = None
    target_margin_rule_id: int | None = None
    price_after_margin: int | None = None
    occupancy_multiplier_bps: int | None = None
    lead_time_multiplier_bps: int | None = None
    demand_factor_bps: int | None = None
    demand_curve_rule_id: int | None = None
    min_profit_halalas: int | None = None
    min_profit_rule_id: int | None = None


@dataclass(frozen=True)
class Quote:
    id: int
    hotel_id: int
    room_type_id: int
    check_in: date
    check_out: date
    rooms: int
    ask_price_total: int
    min_allowed_total: int
    nights: list[NightPrice]
    negotiation_open: bool


def _fetch_cost_per_night(
    conn: psycopg.Connection[Any], hotel_id: int, room_type_id: int, stay_date: date
) -> int:
    row = conn.execute(
        "SELECT cost_per_night FROM allotments "
        "WHERE hotel_id = %s AND room_type_id = %s AND stay_date = %s",
        (hotel_id, room_type_id, stay_date),
    ).fetchone()
    if row is None:
        raise AllotmentNotFoundError(
            f"no allotment for hotel {hotel_id}/room type {room_type_id} on {stay_date}"
        )
    return int(row[0])


def _fetch_active_override(
    conn: psycopg.Connection[Any],
    hotel_id: int,
    room_type_id: int,
    stay_date: date,
    now: datetime,
) -> tuple[int, int] | None:
    row = conn.execute(
        "SELECT ask_price_override, min_allowed_override FROM price_overrides "
        "WHERE hotel_id = %s AND room_type_id = %s AND stay_date = %s "
        "AND expires_at > %s",
        (hotel_id, room_type_id, stay_date, now),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), int(row[1])


def _price_one_night(
    conn: psycopg.Connection[Any],
    hotel_id: int,
    room_type_id: int,
    stay_date: date,
    lead_days: int,
    now: datetime,
) -> NightPrice:
    season_id = resolve_season_id(conn, stay_date)

    override = _fetch_active_override(conn, hotel_id, room_type_id, stay_date, now)
    if override is not None:
        ask, min_allowed = override
        return NightPrice(
            stay_date=stay_date,
            season_id=season_id,
            ask=ask,
            min_allowed=min_allowed,
            override_applied=True,
        )

    rule = resolve_price_rule(conn, hotel_id, room_type_id, season_id)
    cost = _fetch_cost_per_night(conn, hotel_id, room_type_id, stay_date)
    occupancy = compute_occupancy(conn, hotel_id, room_type_id, stay_date)
    demand = compute_demand_factor(rule.demand_curve, occupancy, lead_days)

    # Explicit sequential steps, each floor-rounded, matching exactly what
    # gets recorded in quotes.nights — the logged trail and the computed
    # price are the same arithmetic, not a separate approximation of it.
    markup_bps = _BPS_SCALE + rule.target_margin_bps
    price_after_margin = cost * markup_bps // _BPS_SCALE
    ask = price_after_margin * demand.combined_bps // _BPS_SCALE

    min_profit_halalas = lookup_band_value(
        rule.min_profit_by_lead_time["bands"],
        lead_days,
        min_key="min_lead_days",
        max_key="max_lead_days",
        value_key="min_profit_halalas",
    )
    min_allowed = cost + min_profit_halalas

    if min_allowed > ask:
        raise InconsistentPriceConfigurationError(
            f"hotel {hotel_id}/room type {room_type_id} on {stay_date}: "
            f"min_allowed ({min_allowed}) exceeds ask ({ask}) — target_margin_bps="
            f"{rule.target_margin_bps}, demand_factor_bps={demand.combined_bps}, "
            f"min_profit_halalas={min_profit_halalas} do not clear cost ({cost})"
        )

    return NightPrice(
        stay_date=stay_date,
        season_id=season_id,
        ask=ask,
        min_allowed=min_allowed,
        override_applied=False,
        cost_per_night=cost,
        occupancy=occupancy,
        target_margin_bps=rule.target_margin_bps,
        target_margin_rule_id=rule.target_margin_rule_id,
        price_after_margin=price_after_margin,
        occupancy_multiplier_bps=demand.occupancy_multiplier_bps,
        lead_time_multiplier_bps=demand.lead_time_multiplier_bps,
        demand_factor_bps=demand.combined_bps,
        demand_curve_rule_id=rule.demand_curve_rule_id,
        min_profit_halalas=min_profit_halalas,
        min_profit_rule_id=rule.min_profit_rule_id,
    )


def _night_to_json(night: NightPrice) -> dict[str, Any]:
    """quotes.nights' per-night shape — see db/migrations/0009_quotes_nights_audit.sql
    for the CHECK constraint that enforces this exact either/or shape at
    save time, so it can't silently regress to a thinner record later.
    """
    record: dict[str, Any] = {
        "date": night.stay_date.isoformat(),
        "season_id": night.season_id,
        "ask": night.ask,
        "min_allowed": night.min_allowed,
        "override_applied": night.override_applied,
    }
    if not night.override_applied:
        record.update(
            {
                "cost_per_night": night.cost_per_night,
                "occupancy": night.occupancy,
                "target_margin_bps": night.target_margin_bps,
                "target_margin_rule_id": night.target_margin_rule_id,
                "price_after_margin": night.price_after_margin,
                "occupancy_multiplier_bps": night.occupancy_multiplier_bps,
                "lead_time_multiplier_bps": night.lead_time_multiplier_bps,
                "demand_factor_bps": night.demand_factor_bps,
                "demand_curve_rule_id": night.demand_curve_rule_id,
                "min_profit_halalas": night.min_profit_halalas,
                "min_profit_rule_id": night.min_profit_rule_id,
            }
        )
    return record


def _insert_quote(
    conn: psycopg.Connection[Any],
    hotel_id: int,
    room_type_id: int,
    check_in: date,
    check_out: date,
    rooms: int,
    ask_price_total: int,
    min_allowed_total: int,
    nights: list[NightPrice],
    negotiation_open: bool,
    customer_phone: str | None,
    conversation_id: int | None,
) -> int:
    nights_json = [_night_to_json(night) for night in nights]
    row = conn.execute(
        "INSERT INTO quotes (hotel_id, room_type_id, check_in, check_out, rooms, "
        "ask_price_total, min_allowed_total, nights, negotiation_open, customer_phone, "
        "conversation_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "RETURNING id",
        (
            hotel_id,
            room_type_id,
            check_in,
            check_out,
            rooms,
            ask_price_total,
            min_allowed_total,
            Json(nights_json),
            negotiation_open,
            customer_phone,
            conversation_id,
        ),
    ).fetchone()
    return int(cast(tuple[Any, ...], row)[0])


def compute_quote(
    conn: psycopg.Connection[Any],
    hotel_id: int,
    room_type_id: int,
    check_in: date,
    check_out: date,
    rooms: int,
    now: datetime,
    *,
    customer_phone: str | None = None,
    conversation_id: int | None = None,
) -> Quote:
    """Prices a stay, night by night, and records the quote.

    lead_days (whole calendar days from now to check_in) is computed once
    and applied to every night's demand/min-profit band lookups — the
    lead time that matters is how far ahead the whole booking was made,
    not a per-night recomputation. Per-night occupancy still varies.

    Raises:
        ValueError: now is naive, check_out is not after check_in, rooms
            is not positive, or check_in is in the past.
        AllotmentNotFoundError: a night in the range has no allotment.
        IncompletePriceRuleChainError: no complete price rule resolves
            for some night (most commonly, no global price_rules row).
        NoMatchingBandError: occupancy or lead time is outside its band
            domain -- not for a validly stored config and real inventory
            (see errors.NoMatchingBandError). A sold-out night is priced,
            at the top occupancy band: whether a room is left to sell is
            the inventory's decision (create_hold), not pricing's.
        InconsistentPriceConfigurationError: a computed night's floor
            exceeds its ceiling — the resolved margin/demand don't clear
            the resolved minimum profit, a price_rules misconfiguration.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (UTC)")
    if check_out <= check_in:
        raise ValueError("check_out must be after check_in")
    if rooms <= 0:
        raise ValueError("rooms must be positive")
    if check_in < now.date():
        raise ValueError("check_in must not be in the past")

    lead_days = (check_in - now.date()).days

    nights: list[NightPrice] = []
    ask_price_total = 0
    min_allowed_total = 0

    stay_date = check_in
    while stay_date < check_out:
        night = _price_one_night(
            conn, hotel_id, room_type_id, stay_date, lead_days, now
        )
        nights.append(night)
        ask_price_total += night.ask * rooms
        min_allowed_total += night.min_allowed * rooms
        stay_date += timedelta(days=1)

    check_in_start = datetime.combine(check_in, time.min, tzinfo=UTC)
    negotiation_open = (check_in_start - now) >= LAST_MINUTE_THRESHOLD

    quote_id = _insert_quote(
        conn,
        hotel_id,
        room_type_id,
        check_in,
        check_out,
        rooms,
        ask_price_total,
        min_allowed_total,
        nights,
        negotiation_open,
        customer_phone,
        conversation_id,
    )

    return Quote(
        id=quote_id,
        hotel_id=hotel_id,
        room_type_id=room_type_id,
        check_in=check_in,
        check_out=check_out,
        rooms=rooms,
        ask_price_total=ask_price_total,
        min_allowed_total=min_allowed_total,
        nights=nights,
        negotiation_open=negotiation_open,
    )
