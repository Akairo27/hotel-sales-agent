"""Unit tests for services/agent/output_guard/decision.py — pure matching
logic over a hand-built AllowedAmounts. No database and no model: this is
where the guard's actual allow/block boundary is pinned down.
"""

from __future__ import annotations

from services.agent.output_guard.decision import (
    AMOUNT_BELOW_FLOOR,
    AMOUNT_FOREIGN_CURRENCY,
    AMOUNT_MATCHED,
    AMOUNT_NO_CURRENCY_MARKER,
    AMOUNT_NOT_IN_QUOTES,
    AMOUNT_UNPARSEABLE,
    AllowedAmounts,
    amounts_are_allowed,
    evaluate_amounts,
)
from tests._multiscript_numbers import to_arabic_indic

# Mirrors a real 3-night, 1-room quote: 45,000 halalas/night, 135,000
# total, floor 30,000/night -> 90,000 total. amounts_halalas only ever
# contains what quote_to_tool_result actually sends the model — the
# total and each night's ask, never the floor.
_ALLOWED = AllowedAmounts(
    quote_ids=(1,),
    amounts_halalas=frozenset({45_000, 135_000}),
    floor_halalas=30_000,
)

_NO_QUOTES = AllowedAmounts(
    quote_ids=(), amounts_halalas=frozenset(), floor_halalas=None
)


def test_a_reply_with_no_numbers_is_allowed() -> None:
    findings = evaluate_amounts("We have availability for those dates!", _ALLOWED)
    assert findings == ()
    assert amounts_are_allowed(findings) is True


def test_a_reply_quoting_the_exact_total_is_allowed() -> None:
    findings = evaluate_amounts("Your total is 1,350.00 SAR", _ALLOWED)
    assert [f.reason for f in findings] == [AMOUNT_MATCHED]
    assert amounts_are_allowed(findings) is True


def test_a_reply_quoting_an_exact_per_night_price_is_allowed() -> None:
    findings = evaluate_amounts("It's 450.00 SAR per night", _ALLOWED)
    assert amounts_are_allowed(findings) is True


def test_a_reply_quoting_both_the_night_price_and_the_total_is_allowed() -> None:
    findings = evaluate_amounts("450.00 SAR per night, 1,350.00 SAR total.", _ALLOWED)
    assert [f.reason for f in findings] == [AMOUNT_MATCHED, AMOUNT_MATCHED]


def test_a_total_the_model_computed_itself_is_blocked() -> None:
    """CLAUDE.md rule 1 caught directly: half the real total (a plausible
    but never-quoted "discount") does not match any amount the model was
    ever actually given."""
    findings = evaluate_amounts("I can do 900.00 SAR for you", _ALLOWED)
    assert [f.reason for f in findings] == [AMOUNT_NOT_IN_QUOTES]
    assert amounts_are_allowed(findings) is False


def test_a_per_room_subtotal_the_model_computed_is_blocked() -> None:
    """ask_price_total is the grand total across rooms and nights;
    night.ask is per-room-per-night. Their product (a 2-room case) is
    not itself an allowed amount, even though it is arithmetically
    correct."""
    two_room_quote = AllowedAmounts(
        quote_ids=(1,),
        amounts_halalas=frozenset({45_000, 270_000}),
        floor_halalas=30_000,
    )
    findings = evaluate_amounts(
        "That's 900.00 SAR per night for 2 rooms", two_room_quote
    )
    assert findings[0].reason == AMOUNT_NOT_IN_QUOTES


def test_an_amount_one_halala_off_a_real_quote_is_blocked() -> None:
    findings = evaluate_amounts("1,349.99 SAR", _ALLOWED)
    assert findings[0].reason == AMOUNT_NOT_IN_QUOTES


def test_the_raw_halalas_integer_stated_as_riyals_is_blocked() -> None:
    findings = evaluate_amounts("135000 SAR", _ALLOWED)
    assert findings[0].halalas == 13_500_000
    assert findings[0].reason == AMOUNT_NOT_IN_QUOTES


def test_an_amount_below_every_floor_is_blocked_as_below_floor() -> None:
    findings = evaluate_amounts("1.00 SAR", _ALLOWED)
    assert findings[0].reason == AMOUNT_BELOW_FLOOR


def test_an_unmatched_amount_above_the_lowest_floor_is_blocked_as_not_in_quotes() -> (
    None
):
    findings = evaluate_amounts("1,200.00 SAR", _ALLOWED)
    assert findings[0].reason == AMOUNT_NOT_IN_QUOTES


def test_min_allowed_total_is_never_an_allowed_amount() -> None:
    """Stating the floor itself, verbatim, must still block: the model
    was never shown min_allowed, so a reply that states it exactly is a
    leak, not a legitimate echo."""
    quote_with_floor_90000 = AllowedAmounts(
        quote_ids=(1,),
        amounts_halalas=frozenset({45_000, 135_000}),
        floor_halalas=90_000,
    )
    findings = evaluate_amounts("900.00 SAR", quote_with_floor_90000)
    assert findings[0].reason == AMOUNT_NOT_IN_QUOTES


def test_a_conversation_with_no_quotes_blocks_any_amount() -> None:
    findings = evaluate_amounts("450.00 SAR", _NO_QUOTES)
    assert findings[0].reason == AMOUNT_NOT_IN_QUOTES


def test_a_conversation_with_no_quotes_allows_a_reply_with_no_amounts() -> None:
    findings = evaluate_amounts("Let me check availability for you.", _NO_QUOTES)
    assert amounts_are_allowed(findings) is True


def test_an_amount_from_an_earlier_quote_in_the_same_conversation_is_allowed() -> None:
    """amounts_halalas is a union across every quote in the conversation
    — a customer asking about a second stay should not make the agent's
    earlier, still-true answer look illegitimate."""
    two_quotes = AllowedAmounts(
        quote_ids=(1, 2),
        amounts_halalas=frozenset({45_000, 135_000, 60_000, 300_000}),
        floor_halalas=30_000,
    )
    findings = evaluate_amounts("For 3 nights it was 1,350.00 SAR", two_quotes)
    assert amounts_are_allowed(findings) is True


def test_an_unparseable_marked_amount_is_blocked() -> None:
    findings = evaluate_amounts("1,2,3.4.5 SAR", _ALLOWED)
    assert findings[0].reason == AMOUNT_UNPARSEABLE
    assert amounts_are_allowed(findings) is False


def test_every_offending_amount_is_reported_not_just_the_first() -> None:
    findings = evaluate_amounts("I can go down to 1,200.00 then 1,100.00 SAR", _ALLOWED)
    assert len(findings) == 2
    assert all(f.reason == AMOUNT_NOT_IN_QUOTES for f in findings)


def test_one_valid_and_one_invalid_amount_still_blocks() -> None:
    findings = evaluate_amounts(
        "The night is 450.00 SAR but I can do 900.00 SAR total", _ALLOWED
    )
    assert [f.reason for f in findings] == [AMOUNT_MATCHED, AMOUNT_NOT_IN_QUOTES]
    assert amounts_are_allowed(findings) is False


def test_a_real_total_with_a_foreign_currency_label_is_blocked() -> None:
    """ "1,350.00 USD" is the *real* total, just relabelled — decision.py
    must block it as AMOUNT_FOREIGN_CURRENCY regardless of the value
    matching, not report a clean AMOUNT_MATCHED."""
    findings = evaluate_amounts("That's 1,350.00 USD", _ALLOWED)
    assert [f.reason for f in findings] == [AMOUNT_FOREIGN_CURRENCY]
    assert amounts_are_allowed(findings) is False


def test_a_real_total_with_no_currency_marker_at_all_is_blocked() -> None:
    """The other half of the closed gap: no enumeration of foreign
    currencies can ever be complete, so a marker-less real number
    ("1,350.00" with no currency word at all) must also block, not pass
    as a match."""
    findings = evaluate_amounts("Your total is 1,350.00", _ALLOWED)
    assert [f.reason for f in findings] == [AMOUNT_NO_CURRENCY_MARKER]
    assert amounts_are_allowed(findings) is False


def test_an_unlisted_currency_label_blocks_the_same_way_as_no_label() -> None:
    """No deny-list can enumerate every world currency — "złoty" proves
    the require-a-SAR-marker mechanism, not the deny-list, is what closes
    this: an unlisted currency is not "foreign_currency" (nothing on the
    deny-list matched), it is "no_currency_marker" (no SAR marker either),
    and it still blocks."""
    findings = evaluate_amounts("1,350.00 złoty", _ALLOWED)
    assert [f.reason for f in findings] == [AMOUNT_NO_CURRENCY_MARKER]


def test_a_marker_less_amount_that_does_not_match_is_still_not_in_quotes() -> None:
    """AMOUNT_NO_CURRENCY_MARKER only fires on the would-otherwise-match
    path — a marker-less amount that is simply wrong keeps reporting
    AMOUNT_NOT_IN_QUOTES, unchanged from before this PR."""
    findings = evaluate_amounts("Your total is 900.00", _ALLOWED)
    assert [f.reason for f in findings] == [AMOUNT_NOT_IN_QUOTES]


def test_an_unparseable_foreign_labelled_amount_is_still_unparseable() -> None:
    """Reason precedence: unparseable is checked before foreign-currency
    in evaluate_amounts's per-candidate chain, unchanged from before this
    PR — a malformed run has no reliable value to reason about at all,
    foreign-labelled or not."""
    findings = evaluate_amounts("1,2,3.4.5 USD", _ALLOWED)
    assert [f.reason for f in findings] == [AMOUNT_UNPARSEABLE]


def test_the_indonesian_rendering_of_a_valid_price_is_allowed() -> None:
    findings = evaluate_amounts("Total: 1.350,00 SAR", _ALLOWED)
    assert amounts_are_allowed(findings) is True


def test_the_arabic_indic_rendering_of_a_valid_price_is_allowed() -> None:
    findings = evaluate_amounts(f"{to_arabic_indic('1,350.00')} ريال", _ALLOWED)
    assert amounts_are_allowed(findings) is True
