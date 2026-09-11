"""Extracts candidate financial amounts from free text — the first stage
of the output guard (ARCHITECTURE.md §7, CLAUDE.md rule 8).

Pure text processing: no I/O, no database, no knowledge of any specific
conversation or quote. Given arbitrary text (a candidate reply the model
produced), finds every substring that could plausibly be a stated price
and normalizes each one to an integer halalas value — the only unit
CLAUDE.md rule 5 allows money to exist in.

A digit run only qualifies as a candidate if it is either:
  1. adjacent to a currency marker — SAR (riyal, ريال, ﷼, ...) or a
     foreign one (USD, $, دولار, ...), or
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

"Adjacent" means nothing but a short run of punctuation/whitespace sits
between the marker and the digit run — a letter or digit in between means
they are not actually adjacent. This is what keeps "1,350.00 SAR for 3
nights" from also flagging the "3": the gap between "SAR" and "3" is
" for ", which contains letters, so it fails the adjacency check even
though "3" sits inside the old fixed-size search window this module used
to have. The check is still bounded to a small span around each digit run
(_MAX_MARKER_SPAN) — not for correctness, only so the check stays O(1)
per digit run instead of rescanning arbitrarily far across the text.

Two currency-aware behaviors sit on top of the marker/shape split:

- A digit run adjacent to a *foreign* currency marker (USD, $, قطري
  ريال, ...) is promoted to a candidate regardless of its shape and is
  never a legitimate amount (services/agent/output_guard/decision.py
  blocks it as AMOUNT_FOREIGN_CURRENCY) — prices in this system are
  always Saudi riyals (prompt.py's prices_are_saudi_riyals_only), so any
  labelled foreign amount is suspicious by construction, not exempted.
- A money-shaped candidate that carries *no* marker at all — neither SAR
  nor foreign — is still a candidate (unchanged), but decision.py treats
  a value match with no marker as AMOUNT_NO_CURRENCY_MARKER rather than a
  clean match. This is what closes a price stated with an unlisted or
  omitted currency ("1,350.00 złoty", or a bare "1,350.00" with no
  currency word at all) — enumerating every world currency can never be
  complete, but requiring the one currency this system actually uses is.

Known, documented gap: a bare, ungrouped integer with no decimal fraction
and no marker ("I can do it for 900") is still not money-shaped and still
never becomes a candidate at all — no shape-based or marker-based rule
here can see it, since there is nothing to detect. Closing it needed the
model to be required to state a currency word with every price it gives —
a prompt.py change (price_currency_word), not an extraction heuristic.

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

# How many characters of pure punctuation/whitespace may separate a
# marker from the digit run it marks. This is not "search a window for
# any marker" like this module used to do — see _classify_marker — it
# only bounds how far outside a digit run the anchored check needs to
# look, so that check stays O(1) instead of O(len(text)). Real SAR/
# foreign-currency renderings never have more than a space or two of
# punctuation between the number and its currency word.
_MAX_MARKER_GAP = 8

# The longest marker phrase this module recognizes ("american dollars",
# 16 characters) plus _MAX_MARKER_GAP, rounded up with margin for
# multi-space gaps. Bounds the before/after slices _classify_marker
# looks at.
_MAX_MARKER_SPAN = 32

_GAP = rf"[^\w]{{0,{_MAX_MARKER_GAP}}}"

# --- SAR markers -------------------------------------------------------
# Word-bounded so "SAR" cannot match inside an unrelated word (e.g. a
# name). Case-insensitive; Arabic has no case, so its markers are plain
# substrings instead (no \b — Arabic morphology makes enumerating every
# inflected suffix impractical, and the stem alone has negligible
# collision risk against unrelated Arabic text, same reasoning this
# module has always used).
_SAR_LATIN_ALTS = r"saudi\s+riyals?|saudi\s+rials?|SAR|riyals?|rials?"

# "ريال" (Arabic yeh, U+064A) is the Saudi riyal's own word. "ریال"
# (Farsi yeh, U+06CC) is a deliberately separate entry, not a typo:
# unicodedata.normalize("NFKC", "﷼") produces exactly that spelling —
# verified directly against a real interpreter, not assumed — so a
# marker list built only from the "obvious" Arabic yeh spelling would
# silently fail to recognize the rial sign once normalize_for_scanning
# has already run. "ريال سعودي" is listed explicitly even though bare
# "ريال" already covers most orderings, for the marker-before-the-number
# case, where only the text ending right at the number is checked.
_SAR_ARABIC_MARKERS: tuple[str, ...] = ("ريال سعودي", "ريال", "ریال", "ر.س")
_SAR_ARABIC_ALTS = "|".join(re.escape(marker) for marker in _SAR_ARABIC_MARKERS)

_SAR_BODY = rf"(?:\b(?:{_SAR_LATIN_ALTS})\b|{_SAR_ARABIC_ALTS})"
_SAR_BEFORE_PATTERN = re.compile(rf"{_SAR_BODY}{_GAP}\Z", re.IGNORECASE)
_SAR_AFTER_PATTERN = re.compile(rf"{_GAP}{_SAR_BODY}", re.IGNORECASE)

# --- Foreign-currency markers -------------------------------------------
# A price adjacent to any of these is blocked outright, regardless of its
# value (decision.AMOUNT_FOREIGN_CURRENCY) — this system never
# legitimately states a non-SAR price. Riyals/rials qualified by a
# nationality other than Saudi are a different currency spelled with the
# base SAR word ("qatari riyal", "ريال قطري", ...) — _classify_marker
# checks foreign markers before SAR ones, so a qualified phrase is never
# miscounted as SAR even though it contains "riyal"/"ريال" as a
# substring.
_FOREIGN_LATIN_ALTS = (
    r"US\s+dollars?|american\s+dollars?|"
    r"qatari\s+riyals?|omani\s+rials?|yemeni\s+rials?|iranian\s+rials?|"
    r"USD|EUR|GBP|IDR|AED|KWD|BHD|QAR|OMR|JPY|CNY|INR|TRY|EGP|PKR|MYR|"
    r"dollars?|dolar|euros?|pounds?|dirhams?|dinars?|rupiah|rupees?|yen|lira"
)

# Symbols are already non-word characters, so they need no \b guard —
# they cannot accidentally match "inside" a word.
_FOREIGN_SYMBOLS: tuple[str, ...] = ("$", "€", "£", "¥", "₹")
_FOREIGN_SYMBOL_ALTS = "|".join(re.escape(symbol) for symbol in _FOREIGN_SYMBOLS)

_FOREIGN_ARABIC_MARKERS: tuple[str, ...] = (
    "ريال قطري",
    "ريال عماني",
    "ريال يمني",
    "ريال إيراني",
    "دولار",
    "يورو",
    "جنيه",
    "درهم",
    "دينار",
    "روبية",
    "روبيه",
    "ليرة",
)
_FOREIGN_ARABIC_ALTS = "|".join(re.escape(marker) for marker in _FOREIGN_ARABIC_MARKERS)

# "Rp" (Indonesian Rupiah) only gets a leading \b, never a trailing one:
# "\bRp\b" fails on "Rp1.350.000" — verified against a real interpreter —
# because "p" and "1" are both word characters, so no boundary exists
# between them, and the real Indonesian rendering is always glued to the
# digits with no space. Kept case-sensitive via the scoped (?-i:...) flag
# group — "rp" lowercase collides too easily with ordinary text ("corp",
# "warp") to treat case-insensitively, unlike the fully-spelled words
# above where \b already does that job.
_FOREIGN_BODY = (
    rf"(?:\b(?:{_FOREIGN_LATIN_ALTS})\b"
    rf"|{_FOREIGN_SYMBOL_ALTS}"
    rf"|\b(?-i:Rp)"
    rf"|{_FOREIGN_ARABIC_ALTS})"
)
_FOREIGN_BEFORE_PATTERN = re.compile(rf"{_FOREIGN_BODY}{_GAP}\Z", re.IGNORECASE)
_FOREIGN_AFTER_PATTERN = re.compile(rf"{_GAP}{_FOREIGN_BODY}", re.IGNORECASE)


@dataclass(frozen=True)
class CandidateAmount:
    """One digit run the text scan considered a plausible stated price.

    halalas is None when the run's shape could not be resolved to a
    number at all (see parse_amount_to_halalas) — a genuinely malformed
    or adversarially mangled amount, not merely an unusual but valid
    rendering.

    has_currency_marker is True only for a SAR marker. foreign_currency_
    marker holds the exact matched foreign-currency text when a foreign
    marker sits adjacent (kept for the escalation record), and is None
    otherwise — including when a SAR marker is what qualified the
    candidate. The two are mutually exclusive: see _classify_marker.
    """

    raw: str
    halalas: int | None
    has_currency_marker: bool
    foreign_currency_marker: str | None


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


def _classify_marker(text: str, start: int, end: int) -> tuple[bool, str | None]:
    """Whether a SAR marker and/or a foreign-currency marker sits
    immediately beside the digit run at [start, end) in text.

    "Immediately beside" means an anchored match against a small slice
    on each side, not "found somewhere nearby" — see the module
    docstring for why that distinction is what keeps a stray digit
    elsewhere in the sentence from being wrongly marked.

    Foreign wins when both a SAR and a foreign marker are adjacent (e.g.
    "1,350.00 SAR / USD") — decision.py treats any foreign marker as an
    outright block regardless of value, so ambiguity must resolve toward
    blocking, not allowing.

    Returns (has_sar_marker, foreign_marker_text). foreign_marker_text is
    the exact matched substring when a foreign marker won, else None.
    """
    before = text[max(0, start - _MAX_MARKER_SPAN) : start]
    after = text[end : end + _MAX_MARKER_SPAN]

    foreign_match = _FOREIGN_BEFORE_PATTERN.search(
        before
    ) or _FOREIGN_AFTER_PATTERN.match(after)
    if foreign_match:
        return False, foreign_match.group().strip()

    has_sar_marker = bool(
        _SAR_BEFORE_PATTERN.search(before) or _SAR_AFTER_PATTERN.match(after)
    )
    return has_sar_marker, None


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

    Two linear passes (find digit runs, then check a bounded span around
    each for a marker) rather than one combined regex: a single
    alternation pattern measured roughly 4 seconds against a 40KB
    adversarial input from quadratic backtracking in testing, while this
    two-pass form measures a few milliseconds on the same input — a
    correctness requirement here, not a style preference, since this
    function's input is attacker-influenceable model output. The marker
    check itself is O(1) per digit run (see _MAX_MARKER_SPAN), so this
    stays linear overall.
    """
    normalized = normalize_for_scanning(text)
    candidates: list[CandidateAmount] = []
    for match in _DIGIT_RUN.finditer(normalized):
        raw = match.group()
        has_sar_marker, foreign_marker = _classify_marker(
            normalized, match.start(), match.end()
        )
        is_marked = has_sar_marker or foreign_marker is not None
        if not is_marked and not _is_money_shaped(raw):
            continue
        candidates.append(
            CandidateAmount(
                raw=raw,
                halalas=parse_amount_to_halalas(raw),
                has_currency_marker=has_sar_marker,
                foreign_currency_marker=foreign_marker,
            )
        )
    return tuple(candidates)
