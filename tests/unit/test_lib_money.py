from __future__ import annotations

import pytest

from lib.money import (
    ARABIC_RIYAL,
    format_halalas_as_arabic_riyal,
    format_halalas_as_sar,
)


@pytest.mark.parametrize(
    ("amount_halalas", "expected"),
    [
        (0, "0.00 SAR"),
        (5, "0.05 SAR"),
        (100, "1.00 SAR"),
        (1250, "12.50 SAR"),  # CLAUDE.md rule 5's own example.
        (125_000, "1,250.00 SAR"),
        (1_000_000_00, "1,000,000.00 SAR"),
    ],
)
def test_format_halalas_as_sar(amount_halalas: int, expected: str) -> None:
    assert format_halalas_as_sar(amount_halalas) == expected


def test_format_halalas_as_sar_rejects_negative() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        format_halalas_as_sar(-1)


@pytest.mark.parametrize("amount_halalas", [0, 5, 1250, 125_000, 1_000_000_00])
def test_format_halalas_as_arabic_riyal_is_the_same_number_with_the_arabic_word(
    amount_halalas: int,
) -> None:
    """Owner decision (2026-09-30): an Arabic reply says ريال, never SAR --
    same digits (Western), same grouping, only the word differs."""
    sar = format_halalas_as_sar(amount_halalas)
    assert format_halalas_as_arabic_riyal(amount_halalas) == (
        sar.removesuffix(" SAR") + " " + ARABIC_RIYAL
    )


def test_format_halalas_as_arabic_riyal_renders_with_western_digits() -> None:
    assert format_halalas_as_arabic_riyal(125_000) == "1,250.00 ريال"


def test_format_halalas_as_arabic_riyal_rejects_negative() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        format_halalas_as_arabic_riyal(-1)
