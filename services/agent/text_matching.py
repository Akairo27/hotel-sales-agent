"""The one normalization for matching a customer's or the model's words
against a fixed list: the bare-yes matcher and the booking-offer marker
(services/agent/booking_buttons.py), and the output guard's
booking-claim phrases (services/agent/output_guard/booking_claims.py).

Arabic spelling variants are collapsed with the same letter mapping
search_hotels applies in the database (services/agent/llm/dispatch.py
imports it from here), plus tatweel, so a phrase matches however it is
decorated. No project imports: the display helpers in
services/agent/llm/quote_display.py use it too.
"""

from __future__ import annotations

import unicodedata

# Arabic spelling normalization: collapses spelling variants a customer's
# own typing is likely to produce (alef with/without hamza, taa marbuta
# vs. haa, alef maksura vs. yaa) and strips tashkeel diacritics. Used in
# the database by search_hotels (services/agent/llm/dispatch.py, as
# translate() arguments) and in Python by normalize_for_matching below.
#
# Built via chr(), not string literals: a literal Arabic character here
# is exactly what RUF001 (ambiguous-unicode-character) exists to flag on
# an isolated single-letter string (ordinary Arabic prose is long enough
# that ruff never flags it), and, unlike an
# escape sequence, ruff's own formatter cannot silently rewrite a chr()
# call back into a raw glyph. Named by their Unicode character name, not
# transliterated, so each mapping is checkable against the Unicode
# standard directly.
_ALEF_HAMZA_ABOVE = chr(0x0623)  # ARABIC LETTER ALEF WITH HAMZA ABOVE
_ALEF_HAMZA_BELOW = chr(0x0625)  # ARABIC LETTER ALEF WITH HAMZA BELOW
_ALEF_MADDA_ABOVE = chr(0x0622)  # ARABIC LETTER ALEF WITH MADDA ABOVE
_ALEF_WASLA = chr(0x0671)  # ARABIC LETTER ALEF WASLA
_BARE_ALEF = chr(0x0627)  # ARABIC LETTER ALEF
_TAA_MARBUTA = chr(0x0629)  # ARABIC LETTER TEH MARBUTA
_HAA = chr(0x0647)  # ARABIC LETTER HEH
_ALEF_MAKSURA = chr(0x0649)  # ARABIC LETTER ALEF MAKSURA
_YAA = chr(0x064A)  # ARABIC LETTER YEH
_TASHKEEL = (
    chr(0x064B)  # ARABIC FATHATAN
    + chr(0x064C)  # ARABIC DAMMATAN
    + chr(0x064D)  # ARABIC KASRATAN
    + chr(0x064E)  # ARABIC FATHA
    + chr(0x064F)  # ARABIC DAMMA
    + chr(0x0650)  # ARABIC KASRA
    + chr(0x0651)  # ARABIC SHADDA
    + chr(0x0652)  # ARABIC SUKUN
    + chr(0x0670)  # ARABIC LETTER SUPERSCRIPT ALEF
)  # deleted, not mapped

_ALEF_VARIANTS = _ALEF_HAMZA_ABOVE + _ALEF_HAMZA_BELOW + _ALEF_MADDA_ABOVE + _ALEF_WASLA
ARABIC_NORMALIZE_FROM = _ALEF_VARIANTS + _TAA_MARBUTA + _ALEF_MAKSURA + _TASHKEEL
# Shorter than ARABIC_NORMALIZE_FROM on purpose: translate() deletes any
# trailing `from` characters with no corresponding `to` character, which
# is exactly what _TASHKEEL above needs (removed, not replaced).
ARABIC_NORMALIZE_TO = (_BARE_ALEF * len(_ALEF_VARIANTS)) + _HAA + _YAA

_TATWEEL = chr(0x0640)  # ARABIC TATWEEL
_KEPT_LETTER_COUNT = len(ARABIC_NORMALIZE_TO)
_ARABIC_LETTER_MAPPING = str.maketrans(
    ARABIC_NORMALIZE_FROM[:_KEPT_LETTER_COUNT],
    ARABIC_NORMALIZE_TO,
    ARABIC_NORMALIZE_FROM[_KEPT_LETTER_COUNT:] + _TATWEEL,
)


def normalize_for_matching(text: str) -> str:
    """NFKC, lower case, Arabic spelling variants collapsed (tatweel and
    diacritics removed, alef forms unified, taa marbuta and alef maksura
    mapped as search_hotels maps them), and every run of whitespace made
    one space, trimmed."""
    folded = unicodedata.normalize("NFKC", text).lower()
    return " ".join(folded.translate(_ARABIC_LETTER_MAPPING).split())
