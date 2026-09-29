"""services/pricing/bands.py — pure band lookup. No database: the price_rules
CHECK constraints (see db/migrations/0006_price_rules.sql) are what
guarantee a *stored* band list has this shape; this module just looks
values up in it.
"""

from __future__ import annotations

from typing import Any

import pytest

from services.pricing.bands import lookup_band_value
from services.pricing.errors import NoMatchingBandError

_LEAD_TIME_BANDS: list[dict[str, Any]] = [
    {"min_lead_days": 0, "max_lead_days": 5, "value": 100},
    {"min_lead_days": 5, "max_lead_days": None, "value": 50},
]


def _lookup(value: float) -> int:
    return lookup_band_value(
        _LEAD_TIME_BANDS,
        value,
        min_key="min_lead_days",
        max_key="max_lead_days",
        value_key="value",
    )


def test_lookup_band_value_start_is_inclusive() -> None:
    assert _lookup(0) == 100


def test_lookup_band_value_within_first_band() -> None:
    assert _lookup(4) == 100


def test_lookup_band_value_end_is_exclusive() -> None:
    assert _lookup(5) == 50


def test_lookup_band_value_open_ended_band_has_no_upper_bound() -> None:
    assert _lookup(10_000) == 50


def test_lookup_band_value_raises_when_value_is_out_of_range() -> None:
    with pytest.raises(NoMatchingBandError):
        _lookup(-1)


def test_lookup_band_value_reusable_for_occupancy_shaped_bands() -> None:
    occupancy_bands = [
        {"min": 0.0, "max": 0.5, "multiplier_bps": 10_000},
        {"min": 0.5, "max": 1.0, "multiplier_bps": 15_000},
    ]
    assert (
        lookup_band_value(
            occupancy_bands,
            0.7,
            min_key="min",
            max_key="max",
            value_key="multiplier_bps",
        )
        == 15_000
    )


# Occupancy's domain is closed at 1 (migration 0006): the band whose max is 1
# must contain 1.0 itself -- a sold-out night.
_OCCUPANCY_BANDS: list[dict[str, Any]] = [
    {"min": 0.0, "max": 0.5, "multiplier_bps": 10_000},
    {"min": 0.5, "max": 1, "multiplier_bps": 15_000},
]


def _occupancy_lookup(value: float, *, closed_domain_end: float | None = 1) -> int:
    return lookup_band_value(
        _OCCUPANCY_BANDS,
        value,
        min_key="min",
        max_key="max",
        value_key="multiplier_bps",
        closed_domain_end=closed_domain_end,
    )


def test_lookup_band_value_closed_domain_end_belongs_to_the_band_ending_there() -> None:
    assert _occupancy_lookup(1.0) == 15_000


def test_lookup_band_value_interior_boundary_stays_exclusive_with_a_closed_end() -> (
    None
):
    assert _occupancy_lookup(0.4999) == 10_000
    assert _occupancy_lookup(0.5) == 15_000


def test_lookup_band_value_domain_end_is_exclusive_unless_declared_closed() -> None:
    with pytest.raises(NoMatchingBandError):
        _occupancy_lookup(1.0, closed_domain_end=None)


def test_lookup_band_value_raises_above_a_closed_domain_end() -> None:
    with pytest.raises(NoMatchingBandError):
        _occupancy_lookup(1.01)
