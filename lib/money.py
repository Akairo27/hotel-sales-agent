"""The one integer-money display helper — CLAUDE.md rule 5 and §5.

All money in this system is an integer count of halalas (`1250` means
12.50 SAR — CLAUDE.md's own example). The only place that integer may be
turned into a human-readable string for a channel the model reads or
writes is here: CLAUDE.md rule 1 forbids the model computing or converting
a price, so services/agent/llm/dispatch.py calls this instead of handing
the model a raw integer and a unit to divide by itself.
"""

from __future__ import annotations

_HALALAS_PER_SAR = 100


def format_halalas_as_sar(amount_halalas: int) -> str:
    """Renders an integer halalas amount as "1,250.00 SAR".

    Raises:
        ValueError: amount_halalas is negative — no display path in this
            system ever needs to render a negative price.
    """
    if amount_halalas < 0:
        raise ValueError(f"amount_halalas must not be negative, got {amount_halalas}")
    sar, halalas = divmod(amount_halalas, _HALALAS_PER_SAR)
    return f"{sar:,}.{halalas:02d} SAR"
