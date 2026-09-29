"""Exception types raised by services/pricing.

Mirrors services/inventory/errors.py's pattern — every exception maps to
a specific, expected failure mode described in ARCHITECTURE.md, never a
bare Exception, per CLAUDE.md §2.
"""

from __future__ import annotations


class PricingError(Exception):
    """Base class for every exception this package raises."""


class NoApplicableSeasonError(PricingError):
    """Raised when a date matches no season and no default season exists
    to fall back to.

    ARCHITECTURE.md requires exactly one default season to always exist
    ("لا يمكن أن يوجد تاريخ بلا سعر") — this is the failure mode for when
    that guarantee has been violated (e.g. an empty seasons table), not
    something normal resolution is expected to hit.
    """


class IncompletePriceRuleChainError(PricingError):
    """Raised when a field (target_margin_bps, min_profit_by_lead_time, or
    demand_curve) resolves to NULL at every scope in the inheritance
    chain — global -> season -> hotel -> room_type.

    Unlike a season gap, there is no safe default to fall back to here:
    a missing margin or profit floor has no sanctioned stand-in value.
    This means no global price_rules row is configured at all — the DB
    guarantees a *present* global row is complete
    (price_rules_global_is_complete), but not that one exists.
    """


class AllotmentNotFoundError(PricingError):
    """Raised when a stay date has no allotment (so no cost_per_night) for
    the given hotel and room type — the night cannot be priced.

    Deliberately not shared with services.inventory.errors — the two
    services own their own exceptions even though the underlying
    condition (missing allotment) is the same one.
    """


class InconsistentPriceConfigurationError(PricingError):
    """Raised when a computed night's min_allowed exceeds its ask_price —
    the resolved price rule's margin/demand combination doesn't clear the
    resolved minimum profit floor.

    This is a price_rules misconfiguration, not a bug in the
    computation, and must be caught before it reaches the quotes table's
    own quotes_min_allowed_not_above_ask CHECK — that constraint only
    guards the summed totals, and a per-night violation can in principle
    be masked by other nights in the same stay, so it is checked here
    per night as well.
    """


class NoMatchingBandError(PricingError):
    """Raised when an occupancy or lead-time value falls outside every
    band in a demand_curve or min_profit_by_lead_time config.

    Does not happen for a valid config and real inventory: the price_rules
    CHECK constraints guarantee coverage of lead time from 0 upward and of
    occupancy from 0 to 1 inclusive (a sold-out night included, since
    2026-09-29 -- before that, lookup_band_value left occupancy 1.0 outside
    every band). So this signals a value outside its domain -- a negative
    lead time (a check_in in the past) or occupancy above 1 -- that is, a
    genuine data problem, not a normal pricing outcome.
    """
