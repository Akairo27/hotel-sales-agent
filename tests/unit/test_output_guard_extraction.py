"""Unit tests for services/agent/output_guard/extraction.py — pure text
processing, no database and no model involved. These are the tests that
pin the number-normalization algorithm itself: which digit runs qualify
as candidate financial amounts, and what halalas value each resolves to.
"""

from __future__ import annotations

import time

import pytest

from lib.money import format_halalas_as_sar
from services.agent.output_guard.extraction import (
    extract_candidate_amounts,
    normalize_for_scanning,
    parse_amount_to_halalas,
)
from tests._multiscript_numbers import (
    RIAL_SIGN,
    RIGHT_TO_LEFT_MARK,
    ZERO_WIDTH_SPACE,
    to_arabic_indic,
    to_extended_arabic_indic,
    to_fullwidth,
)

_HALALAS_SAMPLES = (0, 1, 50, 999, 45_000, 125_000, 123_456_789)


@pytest.mark.parametrize("halalas", _HALALAS_SAMPLES)
def test_every_price_format_halalas_as_sar_can_produce_round_trips_back(
    halalas: int,
) -> None:
    """Ties the guard's parser to the real money-formatting helper: a
    change to either side that breaks this invariant is exactly the bug
    this module exists to never have."""
    rendered = format_halalas_as_sar(halalas)
    (candidate,) = extract_candidate_amounts(rendered)
    assert candidate.halalas == halalas


def test_indonesian_separator_convention_normalizes_to_the_same_halalas() -> None:
    (grouped,) = extract_candidate_amounts("1.250,00 SAR")
    (ungrouped,) = extract_candidate_amounts("Total: 1.350,00 SAR")
    assert grouped.halalas == 125_000
    assert ungrouped.halalas == 135_000


def test_arabic_indic_digits_and_separators_normalize_to_the_same_halalas() -> None:
    (with_decimal,) = extract_candidate_amounts(f"{to_arabic_indic('1,350.00')} ريال")
    (whole_riyals,) = extract_candidate_amounts(f"{to_arabic_indic('1250')} ر.س")
    assert with_decimal.halalas == 135_000
    assert whole_riyals.halalas == 125_000


def test_extended_arabic_indic_digits_normalize() -> None:
    (candidate,) = extract_candidate_amounts(
        f"{to_extended_arabic_indic('1250')} {RIAL_SIGN}"
    )
    assert candidate.halalas == 125_000


def test_the_rial_sign_is_recognized_after_nfkc_normalization() -> None:
    """The rial sign NFKC-normalizes to a Farsi-yeh spelling of "riyal",
    not the Arabic-yeh spelling — verified directly against a real
    interpreter. A marker list built only from the "obvious" Arabic
    spelling would silently stop recognizing this sign."""
    candidates = extract_candidate_amounts(
        f"{to_extended_arabic_indic('1250')} {RIAL_SIGN}"
    )
    assert len(candidates) == 1
    assert candidates[0].has_currency_marker is True


def test_fullwidth_digits_and_separators_normalize() -> None:
    (candidate,) = extract_candidate_amounts(f"{to_fullwidth('1,250.00')} SAR")
    assert candidate.halalas == 125_000


def test_a_zero_width_space_between_digits_does_not_split_a_price() -> None:
    (candidate,) = extract_candidate_amounts(f"1,2{ZERO_WIDTH_SPACE}50.00 SAR")
    assert candidate.halalas == 125_000


def test_bidi_marks_around_an_arabic_price_do_not_hide_it() -> None:
    marked = f"{RIGHT_TO_LEFT_MARK}{to_arabic_indic('900.00')} ريال{RIGHT_TO_LEFT_MARK}"
    (candidate,) = extract_candidate_amounts(marked)
    assert candidate.halalas == 90_000


@pytest.mark.parametrize(
    "text",
    [
        "3 nights",
        f"{to_arabic_indic('3')} ليال",
        "2 rooms, 5 stars",
        "350 meters from the Haram",
        "check-in 2026-09-01",
        "tanggal 1.9.2026",
        "01.09.2026",
        "check-in at 14.00",
        "jam 9.30",
        "+966500000001",
        "رقم الحجز 1234567",
        "we have 2 rooms",
    ],
)
def test_non_monetary_numbers_are_not_candidates(text: str) -> None:
    assert extract_candidate_amounts(text) == ()


@pytest.mark.parametrize(
    "text",
    [
        "SAR 1250",
        "1250 SAR",
        "sar1,250.00",
        f"{to_arabic_indic('1250')} ريال",
        "1250 riyals",
        "1250 rial",
    ],
)
def test_a_currency_marker_makes_even_a_bare_integer_a_candidate(text: str) -> None:
    candidates = extract_candidate_amounts(text)
    assert len(candidates) == 1
    assert candidates[0].has_currency_marker is True


def test_currency_marker_does_not_match_inside_an_unrelated_word() -> None:
    """ "SAR" must be word-bounded — otherwise a name like "Sarah" or a
    word like "disregard" (contains "sr") would falsely mark a nearby
    number as financial.
    """
    assert extract_candidate_amounts("Sarah has 3 rooms") == ()
    assert extract_candidate_amounts("please disregard the 3 rooms") == ()


def test_a_grouped_or_two_decimal_number_is_a_candidate_without_any_marker() -> None:
    (grouped,) = extract_candidate_amounts("the total comes to 1,250.00")
    (decimal,) = extract_candidate_amounts("450.00 per night")
    assert grouped.halalas == 125_000
    assert grouped.has_currency_marker is False
    assert grouped.foreign_currency_marker is None
    assert decimal.halalas == 45_000
    assert decimal.has_currency_marker is False
    assert decimal.foreign_currency_marker is None


def test_a_foreign_currency_amount_is_still_a_candidate() -> None:
    """Money-shaped without SAR is still flagged — this system never
    legitimately states a non-SAR amount, so any such number is
    suspicious by construction, not exempted. Strengthened, not merely
    incidental: foreign_currency_marker records exactly which token
    triggered it, which is what lets decision.py block it outright
    (AMOUNT_FOREIGN_CURRENCY) rather than by the coincidence of the value
    also being wrong."""
    (candidate,) = extract_candidate_amounts("about 240.00 USD")
    assert candidate.halalas == 24_000
    assert candidate.foreign_currency_marker == "USD"
    assert candidate.has_currency_marker is False


@pytest.mark.parametrize(
    ("text", "expected_marker"),
    [
        ("1,350.00 USD", "USD"),
        ("1,350.00 EUR", "EUR"),
        ("$1,350.00", "$"),
        ("US$1,350.00", "$"),
        ("€1,350.00", "€"),
        ("Rp1.350.000", "Rp"),
        ("I can do it for 900 USD", "USD"),
        ("360 US dollars", "US dollars"),
        ("1,350.00 دولار", "دولار"),
        ("1,350.00 يورو", "يورو"),
        ("1,350.00 ريال قطري", "ريال قطري"),
        ("1,350.00 Qatari riyal", "Qatari riyal"),
        ("900 omani rial", "omani rial"),
    ],
)
def test_a_foreign_currency_token_promotes_and_marks_the_candidate(
    text: str, expected_marker: str
) -> None:
    """Every class of foreign-currency token this module recognizes: ISO
    code, symbol (bare and compound), the Indonesian Rupiah symbol
    (glued to its digits, no space), a spelled-out word, Arabic words,
    and riyals/rials qualified by a nationality other than Saudi — which
    are a different currency spelled with the SAR marker word, not SAR
    itself. Several of these ("900 USD", "Rp1.350.000") are not
    money-shaped and would be invisible without the marker promoting
    them into candidates at all."""
    (candidate,) = extract_candidate_amounts(text)
    assert candidate.foreign_currency_marker == expected_marker
    assert candidate.has_currency_marker is False


@pytest.mark.parametrize(
    "text",
    ["1,350.00 ريال سعودي", "1,350.00 Saudi riyals", "1,350.00 SAUDI RIYAL"],
)
def test_a_saudi_qualified_riyal_is_sar_not_foreign(text: str) -> None:
    """ "Saudi riyal(s)" must not fall through to the bare "riyal"
    handling by accident and must never be mistaken for a foreign
    qualifier — this is the one nationality-qualified riyal that is
    legitimate."""
    (candidate,) = extract_candidate_amounts(text)
    assert candidate.has_currency_marker is True
    assert candidate.foreign_currency_marker is None


def test_a_foreign_marker_does_not_match_case_insensitively_for_rp() -> None:
    """Rp is deliberately case-sensitive (see the module's comment on
    _FOREIGN_BODY) — "corp 1,350.00" and a lowercase "rp" must not be
    mistaken for the Indonesian Rupiah symbol. The number is still a
    candidate either way, since "1,350.00" is money-shaped on its own;
    what this pins is that neither case counts as foreign."""
    (corp,) = extract_candidate_amounts("corp 1,350.00")
    assert corp.foreign_currency_marker is None
    (lowercase_rp,) = extract_candidate_amounts("rp1.350.000")
    assert lowercase_rp.foreign_currency_marker is None


def test_usd_does_not_match_inside_an_unrelated_word() -> None:
    """Mirrors test_currency_marker_does_not_match_inside_an_unrelated_
    word for the foreign side: "USDA" must not be read as "USD"."""
    (candidate,) = extract_candidate_amounts("1,350.00 USDA")
    assert candidate.foreign_currency_marker is None
    assert candidate.has_currency_marker is False


def test_a_marker_separated_from_the_number_by_a_word_is_not_adjacent() -> None:
    """The false positive this PR fixes: the old fixed-size search
    window let a currency marker "mark" a number elsewhere in the same
    sentence as long as it fell within 12 characters, even across
    intervening words. "for 3 nights" sits between "SAR" and "3" here —
    since that gap contains letters, not just punctuation/whitespace,
    "3" must not be treated as SAR-marked, and (being neither
    money-shaped nor marked) must not be a candidate at all."""
    candidates = extract_candidate_amounts("Your total is 1,350.00 SAR for 3 nights.")
    assert len(candidates) == 1
    assert candidates[0].halalas == 135_000


def test_a_marker_glued_directly_to_the_number_is_still_adjacent() -> None:
    """The zero-gap end of the same spectrum — no punctuation at all
    between the marker and the number must still count."""
    (candidate,) = extract_candidate_amounts("SAR900.00")
    assert candidate.has_currency_marker is True
    assert candidate.halalas == 90_000


@pytest.mark.parametrize(
    "text",
    ["1,2,3.4.5 SAR", "12.3456 SAR", "1,25,000 SAR"],
)
def test_malformed_amounts_next_to_a_marker_are_candidates_that_cannot_be_parsed(
    text: str,
) -> None:
    candidates = extract_candidate_amounts(text)
    assert len(candidates) == 1
    assert candidates[0].halalas is None


def test_parse_amount_to_halalas_treats_a_three_digit_trailing_group_as_grouping() -> (
    None
):
    assert parse_amount_to_halalas("12.345") == 1_234_500


@pytest.mark.parametrize(
    "raw", [".50", "1,", "1,2,3", "2026.09.01", "12.3.45", "1,25,000"]
)
def test_parse_amount_to_halalas_returns_none_for_ambiguous_shapes(raw: str) -> None:
    assert parse_amount_to_halalas(raw) is None


def test_an_ungrouped_two_decimal_price_parses() -> None:
    assert parse_amount_to_halalas("1250.00") == 125_000


def test_multiple_amounts_in_one_reply_are_all_extracted() -> None:
    candidates = extract_candidate_amounts("450.00 SAR per night, 1,350.00 SAR total.")
    assert [c.halalas for c in candidates] == [45_000, 135_000]


def test_a_percentage_is_not_a_candidate() -> None:
    """Documented limitation, not a passing behavior worth celebrating:
    a percentage is not an amount, and this module's contract is
    matching numbers against a quote, not policing every number."""
    assert extract_candidate_amounts("our margin is 20%") == ()


def test_extraction_is_linear_on_a_pathological_input() -> None:
    """Pins the two-pass design (find digit runs, then check a bounded
    window for a marker) against a future "simplification" back into one
    combined alternation regex, which measured roughly 4 seconds against
    this exact input from quadratic backtracking. The input is
    attacker-influenceable model output, so this is a correctness bound,
    not a micro-benchmark."""
    pathological = "1," * 20_000
    start = time.perf_counter()
    extract_candidate_amounts(pathological)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0


def test_normalize_for_scanning_strips_format_characters_but_keeps_letters() -> None:
    assert normalize_for_scanning("A​B") == "AB"
    assert normalize_for_scanning("مرحبا") == "مرحبا"
