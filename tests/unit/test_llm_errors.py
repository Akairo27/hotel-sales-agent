"""Unit tests for services/agent/llm/errors.py's cross-exception
usage-carrying pair — attach_usage_so_far and read_usage_so_far. No
database, no network: this only proves the write site and the read site
agree with each other and with the shared USAGE_SO_FAR_ATTR constant —
the one name either of them ever references for this, so a rename on one
side can never silently stop the other from finding it. See errors.py's
own module docstring for why the mechanism is generic (works on any
exception) rather than a constructor parameter per exception class.
"""

from __future__ import annotations

from services.agent.llm.conversation import UsageTotals
from services.agent.llm.errors import (
    USAGE_SO_FAR_ATTR,
    LlmError,
    attach_usage_so_far,
    read_usage_so_far,
)


def test_read_usage_so_far_returns_none_when_nothing_was_ever_attached() -> None:
    assert read_usage_so_far(LlmError("untouched")) is None


def test_attach_then_read_round_trips_the_same_usage() -> None:
    exc = LlmError("boom")
    usage = UsageTotals(7, 3, 10)

    attach_usage_so_far(exc, usage)

    assert read_usage_so_far(exc) == usage


def test_attach_and_read_use_the_same_shared_attribute_name() -> None:
    """The write site (attach_usage_so_far) and the read site
    (read_usage_so_far) must reference the identical constant, not two
    independently hardcoded string literals that could drift apart —
    proven here in both directions: writing through the raw attribute
    name and reading back through the paired function, and writing
    through the paired function and reading back through the raw name."""
    written_raw = LlmError("boom")
    usage = UsageTotals(1, 1, 2)
    setattr(written_raw, USAGE_SO_FAR_ATTR, usage)
    assert read_usage_so_far(written_raw) == usage

    written_via_function = LlmError("boom")
    attach_usage_so_far(written_via_function, usage)
    assert getattr(written_via_function, USAGE_SO_FAR_ATTR) == usage


def test_attach_usage_so_far_works_on_a_foreign_exception_type() -> None:
    """The whole point of a generic, type-agnostic write site: it must
    work on an exception from a completely different hierarchy (e.g.
    services.pricing.errors' exceptions), not just LlmError subclasses —
    proven here with a plain built-in exception type."""
    exc = ValueError("some exception this package does not own")
    usage = UsageTotals(4, 4, 8)

    attach_usage_so_far(exc, usage)

    assert read_usage_so_far(exc) == usage
