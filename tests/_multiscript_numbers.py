"""Multi-script digit/separator helpers for output-guard tests.

Built entirely from chr() on exact codepoints rather than typed as
literal glyphs, so ruff's ambiguous-character check (RUF001) has nothing
to flag in the test files that use these: real Arabic-Indic, extended
Arabic-Indic, and fullwidth digits are the exact thing
services/agent/output_guard/extraction.py is tested against, not
disguised ASCII lookalikes a typo introduced.

Not a test module itself (no test_ prefix), so pytest does not collect
it — same convention as tests/integration/_seed.py.
"""

from __future__ import annotations


def _digit_block(start_codepoint: int) -> str:
    return "".join(chr(start_codepoint + digit) for digit in range(10))


ARABIC_INDIC_DIGITS = _digit_block(0x0660)  # Arabic-Indic 0-9
EXTENDED_ARABIC_INDIC_DIGITS = _digit_block(0x06F0)  # Persian/Urdu 0-9
FULLWIDTH_DIGITS = _digit_block(0xFF10)  # fullwidth 0-9

ARABIC_DECIMAL_SEPARATOR = chr(0x066B)
ARABIC_THOUSANDS_SEPARATOR = chr(0x066C)
FULLWIDTH_COMMA = chr(0xFF0C)
FULLWIDTH_PERIOD = chr(0xFF0E)
RIAL_SIGN = chr(0xFDFC)
RIGHT_TO_LEFT_MARK = chr(0x200F)
ZERO_WIDTH_SPACE = chr(0x200B)

_TO_ARABIC_INDIC = str.maketrans(
    "0123456789.,",
    ARABIC_INDIC_DIGITS + ARABIC_DECIMAL_SEPARATOR + ARABIC_THOUSANDS_SEPARATOR,
)
_TO_EXTENDED_ARABIC_INDIC = str.maketrans("0123456789", EXTENDED_ARABIC_INDIC_DIGITS)
_TO_FULLWIDTH = str.maketrans(
    "0123456789.,", FULLWIDTH_DIGITS + FULLWIDTH_PERIOD + FULLWIDTH_COMMA
)


def to_arabic_indic(ascii_number: str) -> str:
    """Translates an ASCII number (digits, ".", ",") to Arabic-Indic
    digits with Arabic decimal/thousands separators."""
    return ascii_number.translate(_TO_ARABIC_INDIC)


def to_extended_arabic_indic(ascii_digits: str) -> str:
    """Translates ASCII digits to extended (Persian/Urdu) Arabic-Indic
    digits. Takes no separators — callers needing one use RIAL_SIGN or
    plain ASCII punctuation alongside this."""
    return ascii_digits.translate(_TO_EXTENDED_ARABIC_INDIC)


def to_fullwidth(ascii_number: str) -> str:
    """Translates an ASCII number (digits, ".", ",") to fullwidth
    digits with fullwidth punctuation."""
    return ascii_number.translate(_TO_FULLWIDTH)
