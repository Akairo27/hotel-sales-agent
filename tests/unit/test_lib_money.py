from __future__ import annotations

import pytest

from lib.money import format_halalas_as_sar


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
