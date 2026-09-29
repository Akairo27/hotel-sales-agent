"""services/pricing/demand.py's compute_demand_factor — pure given a
demand_curve dict and (occupancy, lead_days) values. compute_occupancy
itself needs a database (room_night_inventory) and is tested in
tests/integration/test_pricing_demand.py.
"""

from __future__ import annotations

import pytest

from services.pricing.demand import compute_demand_factor
from services.pricing.errors import NoMatchingBandError

_DEMAND_CURVE = {
    "occupancy_bands": [
        {"min": 0.0, "max": 0.5, "multiplier_bps": 10_000},
        {"min": 0.5, "max": 1.0, "multiplier_bps": 15_000},
    ],
    "lead_time_bands": [
        {"min_lead_days": 0, "max_lead_days": 3, "multiplier_bps": 12_000},
        {"min_lead_days": 3, "max_lead_days": None, "multiplier_bps": 10_000},
    ],
}


def test_compute_demand_factor_combines_low_occupancy_and_far_lead_time() -> None:
    factor = compute_demand_factor(_DEMAND_CURVE, 0.2, 10)
    # 1.0x occupancy multiplier * 1.0x lead-time multiplier = 1.0x
    assert factor.occupancy_multiplier_bps == 10_000
    assert factor.lead_time_multiplier_bps == 10_000
    assert factor.combined_bps == 10_000


def test_compute_demand_factor_combines_high_occupancy_and_near_lead_time() -> None:
    factor = compute_demand_factor(_DEMAND_CURVE, 0.8, 1)
    # 1.5x occupancy multiplier * 1.2x lead-time multiplier = 1.8x
    assert factor.occupancy_multiplier_bps == 15_000
    assert factor.lead_time_multiplier_bps == 12_000
    assert factor.combined_bps == 18_000


def test_compute_demand_factor_rounds_down() -> None:
    curve = {
        "occupancy_bands": [{"min": 0, "max": 1, "multiplier_bps": 15_000}],
        "lead_time_bands": [
            {"min_lead_days": 0, "max_lead_days": None, "multiplier_bps": 333}
        ],
    }
    # true value: 15000 * 333 / 10000 = 499.5 -- must floor to 499, not round to 500.
    assert compute_demand_factor(curve, 0.1, 0).combined_bps == 499


def test_compute_demand_factor_prices_a_sold_out_night_at_the_top_band() -> None:
    """Occupancy 1.0 -- reserved + held == total, or a night with no rooms
    at all -- used to fall outside every valid band and raise
    NoMatchingBandError."""
    factor = compute_demand_factor(_DEMAND_CURVE, 1.0, 10)
    assert factor.occupancy_multiplier_bps == 15_000
    assert factor.combined_bps == 15_000


def test_compute_demand_factor_prices_full_occupancy_on_a_single_band_curve() -> None:
    """The live global rule's shape (2026-09-29): one band, 0 to 1, stored
    with an integer max."""
    curve = {
        "occupancy_bands": [{"min": 0, "max": 1, "multiplier_bps": 10_000}],
        "lead_time_bands": [
            {"min_lead_days": 0, "max_lead_days": None, "multiplier_bps": 10_000}
        ],
    }
    assert compute_demand_factor(curve, 1.0, 30).combined_bps == 10_000


def test_compute_demand_factor_just_below_full_occupancy_uses_the_top_band() -> None:
    assert compute_demand_factor(_DEMAND_CURVE, 0.9999, 10).combined_bps == 15_000


def test_compute_demand_factor_rejects_occupancy_above_one() -> None:
    with pytest.raises(NoMatchingBandError):
        compute_demand_factor(_DEMAND_CURVE, 1.01, 10)
