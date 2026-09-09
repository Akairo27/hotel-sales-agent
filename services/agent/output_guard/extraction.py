"""Extracts candidate financial amounts from free text — the first stage
of the output guard (ARCHITECTURE.md §7, CLAUDE.md rule 8).

Pure text processing: no I/O, no database, no knowledge of any specific
conversation or quote. Given arbitrary text (a candidate reply the model
produced), finds every substring that could plausibly be a stated price
and normalizes each one to an integer halalas value — the only unit
CLAUDE.md rule 5 allows money to exist in.

A digit run only qualifies as a candidate if it is either:
  1. adjacent to a currency marker (SAR, ريال, ﷼, ...), or
  2. shaped like money on its own: thousands-grouped digits (1,250 /
     1.250) or a bare number with a two-decimal fraction and a
     three-or-more-digit integer part (450.00).

Rule 2's three-digit floor exists because Indonesian-style clock times
("14.00", "9.30") and dotted dates ("1.9.2026") would otherwise become
false candidates — every SAR amount lib.money.format_halalas_as_sar can
produce has exactly two decimal digits and, once its integer part
reaches four digits, thousands grouping, so this shape is not a
coincidence being avoided; it is the one shape real money never fails to
have.

Known, documented gap: a bare unmarked integer with no separator and no
decimal fraction ("I can do it for 900") is invisible to this module.
Closing it needs the model to be required to state a currency word with
every price it gives — a prompt.py change, not an extraction heuristic —
and is the very next piece of work after this one, not a someday item.

Also out of scope: a percentage, in any script or digit set, is never a
candidate here — it is not an amount.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_HALALAS_PER_SAR = 100

# Separators seen in this system's own SAR rendering (lib/money.py) or a
# faithful Indonesian-style re-rendering of it: "." and "," swap roles
# between the two conventions. The other two are their Arabic
# equivalents (U+066B Arabic decimal separator, U+066C Arabic thousands
# separator) — built via chr() on the exact codepoint rather than typed
# as a literal glyph, so this reads as an exact codepoint rather than as
# a character that merely looks like "," or ".".
_SEPARATOR_CHARS = ".," + chr(0x066B) + chr(0x066C)

# One digit, or a maximal run of digits with interior separators, never
# starting or ending on a separator. \d matches every decimal-digit
# script Python's re module recognizes (ASCII, Arabic-Indic, Extended
# Arabic-Indic, fullwidth, ...), so no per-script character class is
# needed here.
_DIGIT_RUN = re.compile(rf"\d(?:[\d{re.escape(_SEPARATOR_CHARS)}]*\d)?")

# Word-bounded so "SAR" cannot match inside an unrelated word (e.g. a
# name). Case-insensitive; Arabic has no case, so its markers are plain
# substrings instead, checked separately below.
_LATIN_MARKER_PATTERN = re.compile(r"\b(?:SAR|riyals?|rials?)\b", re.IGNORECASE)

# "ريال" (Arabic yeh, U+064A) is matched as a bare substring — Arabic
# morphology makes enumerating every inflected suffix (ريالات، ريالاً،
# ريالا) impractical, and the stem alone has negligible collision risk
# against unrelated Arabic text. "ریال" (Farsi yeh, U+06CC) is a
# deliberately separate entry, not a typo: unicodedata.normalize("NFKC",
# "﷼") produces exactly that spelling — verified directly against a real
# interpreter, not assumed — so a marker list built only from the
# "obvious" Arabic yeh spelling would silently fail to recognize the
# rial sign once normalize_for_scanning has already run.
_ARABIC_MARKERS: tuple[str, ...] = ("ريال", "ریال", "ر.س")

_MARKER_SEARCH_WINDOW = 12


@dataclass(frozen=True)
class CandidateAmount:
    """One digit run the text scan considered a plausible stated price.

    halalas is None when the run's shape could not be resolved to a
    number at all (see parse_amount_to_halalas) — a genuinely malformed
    or adversarially mangled amount, not merely an unusual but valid
    rendering.
    """

    raw: str
    halalas: int | None
    has_currency_marker: bool


def normalize_for_scanning(text: str) -> str:
    """NFKC-normalizes text and drops every Unicode "format" character
    (category Cf: zero-width spaces/joiners, bidi marks, ...).

    Both steps defeat a specific evasion, not a general cleanup pass:
    NFKC folds fullwidth digits and the ﷼ sign into forms the rest of
    this module already recognizes; stripping Cf as a whole category —
    rather than a hand-picked character list — means a zero-width space
    inserted between two digits of a real price cannot fragment it into
    two smaller, non-matching numbers.
    """
    normalized = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Cf")


def _has_nearby_currency_marker(text: str, start: int, end: int) -> bool:
    window_start = max(0, start - _MARKER_SEARCH_WINDOW)
    window_end = min(len(text), end + _MARKER_SEARCH_WINDOW)
    before = text[window_start:start]
    after = text[end:window_end]
    for chunk in (before, after):
        if _LATIN_MARKER_PATTERN.search(chunk):
            return True
        if any(marker in chunk for marker in _ARABIC_MARKERS):
            return True
    return False


def _is_grouped(groups: list[str]) -> bool:
    """Whether a sequence of digit groups looks like a thousands-grouped
    integer: a one-to-three-digit lead group followed by one or more
    exactly-three-digit groups (1,250 / 12,345,678 / 1.250)."""
    return (
        len(groups) >= 2
        and 1 <= len(groups[0]) <= 3
        and all(len(group) == 3 for group in groups[1:])
    )


def _split_riyals_and_fraction(raw: str) -> tuple[list[str], str] | None:
    """Splits a digit run into its riyal groups and fraction digits, or
    None if the run's group shape cannot be resolved at all.

    The trailing group's length is what resolves the en/id separator
    ambiguity, not locale guessing: lib.money.format_halalas_as_sar
    always emits exactly two decimal digits, so a trailing group of
    exactly two digits is unambiguously the fraction, and a trailing
    group of exactly three digits is unambiguously a thousands group —
    no other legitimate SAR rendering produces either shape any other
    way. Any other trailing length, or a non-decimal group anywhere, is
    unparseable.
    """
    groups = re.split(f"[{re.escape(_SEPARATOR_CHARS)}]", raw)
    if any(not group.isdecimal() for group in groups):
        return None
    if len(groups) == 1:
        return groups, ""

    last = groups[-1]
    if len(last) == 2:
        return groups[:-1], last
    if len(last) == 3:
        return groups, ""
    return None


def parse_amount_to_halalas(raw: str) -> int | None:
    """Parses one digit run to an integer halalas value, or None if its
    shape cannot be resolved unambiguously. See
    _split_riyals_and_fraction for the separator-ambiguity resolution.
    """
    split = _split_riyals_and_fraction(raw)
    if split is None:
        return None
    riyal_groups, fraction = split

    if len(riyal_groups) > 1 and not _is_grouped(riyal_groups):
        return None

    riyals = int("".join(riyal_groups))
    return riyals * _HALALAS_PER_SAR + (int(fraction) if fraction else 0)


def _is_money_shaped(raw: str) -> bool:
    """Whether a digit run looks like a price even without a currency
    marker nearby: thousands-grouped, or a two-decimal fraction with a
    three-or-more-digit integer part. See the module docstring for why
    the second shape's length floor matters — it is what keeps clock
    times and dotted dates from becoming false candidates.
    """
    split = _split_riyals_and_fraction(raw)
    if split is None:
        return False
    riyal_groups, fraction = split
    if len(riyal_groups) == 1 and not fraction:
        return False  # a bare, ungrouped, non-decimal run — needs a marker
    if len(riyal_groups) > 1:
        return _is_grouped(riyal_groups)
    return len(fraction) == 2 and len(riyal_groups[0]) >= 3


def extract_candidate_amounts(text: str) -> tuple[CandidateAmount, ...]:
    """Finds every plausible stated price in text and normalizes each to
    halalas. See the module docstring for what qualifies as a candidate.

    Two linear passes (find digit runs, then check a bounded window
    around each for a marker) rather than one combined regex: a single
    alternation pattern measured roughly 4 seconds against a 40KB
    adversarial input from quadratic backtracking in testing, while this
    two-pass form measures a few milliseconds on the same input — a
    correctness requirement here, not a style preference, since this
    function's input is attacker-influenceable model output.
    """
    normalized = normalize_for_scanning(text)
    candidates: list[CandidateAmount] = []
    for match in _DIGIT_RUN.finditer(normalized):
        raw = match.group()
        has_marker = _has_nearby_currency_marker(normalized, match.start(), match.end())
        if not has_marker and not _is_money_shaped(raw):
            continue
        candidates.append(
            CandidateAmount(
                raw=raw,
                halalas=parse_amount_to_halalas(raw),
                has_currency_marker=has_marker,
            )
        )
    return tuple(candidates)
