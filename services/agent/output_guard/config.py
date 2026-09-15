"""Configuration constants for services/agent/output_guard.

MIN_BARE_PRICE_HALALAS is a business assumption about this market, not a
derived or measured value: no real room-night price this system quotes is
expected to ever fall below it (confirmed with the client — a 100 SAR
floor is realistic for hotels near the Haram). It exists so
extraction.py can tell a bare, unmarked integer that is a price ("I can
do it for 900") apart from an unrelated small number in the same reply
(a room count, a night count, a day of month) without any per-conversation
context: those numbers are always far below this floor, while a real
price never is.

Kept in its own module, not as a literal inside extraction.py's matching
logic: if this market's actual minimum price ever changes, this constant
— named, with this comment attached — is what the person making that
change needs to find, not a magic number buried in a regex-adjacent
function.
"""

from __future__ import annotations

# 100.00 SAR. See the module docstring above for why this exact value.
MIN_BARE_PRICE_HALALAS = 10_000
