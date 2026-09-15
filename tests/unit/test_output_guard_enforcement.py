"""Unit tests for services/agent/output_guard/enforcement.py's non-
matching-logic pieces — currently just the customer-facing fallback
message. No database: enforce_outbound_text itself (the DB-backed half,
including proving this exact message is always allowed) is covered in
tests/integration/test_output_guard.py.
"""

from __future__ import annotations

from services.agent.output_guard.enforcement import OUTPUT_GUARD_FALLBACK_MESSAGE

# U+0660-U+0669 — the Arabic-Indic digits, the other digit script this
# system's customers actually write in (see extraction.py's own
# multi-script handling). Checked as its own named test, not folded into
# the ASCII one: the two digit sets are visually unrelated, so an editor
# fixing an ASCII "9" they can see has no reason to also think to check
# for an Arabic-Indic one they might not immediately recognize as a digit.
_ARABIC_INDIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"


def test_output_guard_fallback_message_has_no_ascii_digits() -> None:
    assert not any(
        ch.isascii() and ch.isdigit() for ch in OUTPUT_GUARD_FALLBACK_MESSAGE
    )


def test_output_guard_fallback_message_has_no_arabic_indic_digits() -> None:
    assert not any(ch in _ARABIC_INDIC_DIGITS for ch in OUTPUT_GUARD_FALLBACK_MESSAGE)


def test_output_guard_fallback_message_has_no_digit_in_any_script() -> None:
    """A broader net over the two targeted checks above: str.isdigit() is
    true for every Unicode decimal-digit script Python recognizes
    (Extended Arabic-Indic and fullwidth digits included, not just the
    two scripts this system's customers actually use) — the same
    definition extraction.py's own _DIGIT_RUN regex relies on, so this
    is the check that actually matches what the guard itself would look
    for."""
    assert not any(ch.isdigit() for ch in OUTPUT_GUARD_FALLBACK_MESSAGE)


def test_output_guard_fallback_message_is_bilingual() -> None:
    """Bilingual by design, not language-detected — see enforcement.py's
    own comment on OUTPUT_GUARD_FALLBACK_MESSAGE for why. Pinned here so
    an edit that accidentally drops one half is caught."""
    assert any(ch.isascii() and ch.isalpha() for ch in OUTPUT_GUARD_FALLBACK_MESSAGE)
    assert any("؀" <= ch <= "ۿ" for ch in OUTPUT_GUARD_FALLBACK_MESSAGE)
