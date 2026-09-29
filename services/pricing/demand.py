"""Demand factor evaluation — ARCHITECTURE.md §5's demand factor:
occupancy rate combined with remaining lead time. The curve shape is
entirely data-driven via price_rules.demand_curve (see the phase-2
design decision); this module only evaluates whatever curve is
configured — occupancy multiplier times lead-time multiplier, in basis
points — never a hardcoded business formula.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import psycopg

from services.pricing.bands import lookup_band_value
from services.pricing.errors import AllotmentNotFoundError

_BPS_SCALE = 10_000

# Occupancy runs from 0 to 1 inclusive: 1.0 is a sold-out night (or one with
# no rooms at all), and it belongs to the band whose max is 1 -- the closed
# domain migration 0006's price_rules_is_valid_occupancy_bands enforces.
OCCUPANCY_DOMAIN_END = 1


@dataclass(frozen=True)
class DemandFactor:
    """The two components of the demand factor kept separate, not just
    their product — a quote needs to be able to say *why* demand pushed
    the price up: because of occupancy, lead time, or both."""

    occupancy_multiplier_bps: int
    lead_time_multiplier_bps: int
    combined_bps: int


def compute_occupancy(
    conn: psycopg.Connection[Any], hotel_id: int, room_type_id: int, stay_date: date
) -> float:
    """The fraction of total rooms already reserved or held for one
    night — the "نسبة التصريف" (sell-through rate) input to the demand
    curve. Includes held rooms, not just confirmed ones: a room on hold
    already reduces what is left to sell — the same reserved+held vs.
    total relationship this system's inventory_never_oversold constraint
    reasons about. Exactly 1.0 for a sold-out night and for a night with
    no rooms at all (total 0); never above 1, since that constraint keeps
    reserved + held within total.

    Raises:
        AllotmentNotFoundError: no allotment exists for this night.
    """
    row = conn.execute(
        "SELECT rni.reserved, rni.held, rni.total FROM room_night_inventory rni "
        "JOIN allotments a ON a.id = rni.allotment_id "
        "WHERE a.hotel_id = %s AND a.room_type_id = %s AND rni.stay_date = %s",
        (hotel_id, room_type_id, stay_date),
    ).fetchone()
    if row is None:
        raise AllotmentNotFoundError(
            f"no allotment for hotel {hotel_id}/room type {room_type_id} on {stay_date}"
        )
    reserved, held, total = int(row[0]), int(row[1]), int(row[2])
    if total == 0:
        return 1.0
    return (reserved + held) / total


def compute_demand_factor(
    demand_curve: dict[str, Any], occupancy: float, lead_days: int
) -> DemandFactor:
    """Combines demand_curve's occupancy-based and lead-time-based
    multipliers into one basis-point factor, keeping both inputs
    alongside the combined result for traceability. A sold-out night
    (occupancy 1.0) takes the top occupancy band's multiplier.

    Raises:
        NoMatchingBandError: occupancy is outside 0..1 or lead_days is
            negative (see lookup_band_value) -- never for a valid stored
            curve and a real inventory row.
    """
    occupancy_bps = lookup_band_value(
        demand_curve["occupancy_bands"],
        occupancy,
        min_key="min",
        max_key="max",
        value_key="multiplier_bps",
        closed_domain_end=OCCUPANCY_DOMAIN_END,
    )
    lead_time_bps = lookup_band_value(
        demand_curve["lead_time_bands"],
        lead_days,
        min_key="min_lead_days",
        max_key="max_lead_days",
        value_key="multiplier_bps",
    )
    combined_bps = occupancy_bps * lead_time_bps // _BPS_SCALE
    return DemandFactor(
        occupancy_multiplier_bps=occupancy_bps,
        lead_time_multiplier_bps=lead_time_bps,
        combined_bps=combined_bps,
    )
