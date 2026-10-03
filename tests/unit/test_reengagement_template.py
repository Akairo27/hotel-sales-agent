"""The re-engagement template's configuration, texts and rendering
(services/agent/reengagement_template.py). The database-backed parts -- the
hotel's name and the customer's language -- are covered end to end in
tests/integration/test_webhook_staff_reply.py."""

from __future__ import annotations

import re

import pytest

from services.agent.reengagement_template import (
    DEFAULT_LANGUAGE,
    ENV_TEMPLATE_NO_HOTEL,
    ENV_TEMPLATE_WITH_HOTEL,
    HOTEL_NAME_MAX_CHARS,
    LANGUAGE_CODES,
    ReengagementConfigurationError,
    ReengagementSettings,
    clean_hotel_name,
    load_reengagement_settings,
    reengagement_settings_or_none,
    render_reengagement,
)

_SETTINGS = ReengagementSettings(
    template_with_hotel="reengagement_with_hotel",
    template_no_hotel="reengagement_no_hotel",
)


def test_it_is_off_until_both_template_names_are_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_TEMPLATE_WITH_HOTEL, raising=False)
    monkeypatch.delenv(ENV_TEMPLATE_NO_HOTEL, raising=False)
    assert load_reengagement_settings() is None

    monkeypatch.setenv(ENV_TEMPLATE_WITH_HOTEL, "with_hotel")
    monkeypatch.setenv(ENV_TEMPLATE_NO_HOTEL, "no_hotel")
    assert load_reengagement_settings() == ReengagementSettings(
        template_with_hotel="with_hotel", template_no_hotel="no_hotel"
    )


def test_blank_names_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_TEMPLATE_WITH_HOTEL, "  ")
    monkeypatch.setenv(ENV_TEMPLATE_NO_HOTEL, "")
    assert load_reengagement_settings() is None


@pytest.mark.parametrize(
    ("with_hotel", "no_hotel"),
    [
        pytest.param("with_hotel", "", id="only-the-hotel-one"),
        pytest.param("", "no_hotel", id="only-the-variant"),
        pytest.param("With Hotel", "no_hotel", id="name-not-a-meta-name"),
        pytest.param("with_hotel", "no-hotel", id="hyphen-not-allowed"),
    ],
)
def test_a_half_set_or_malformed_configuration_is_an_error_and_treated_as_off(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    with_hotel: str,
    no_hotel: str,
) -> None:
    monkeypatch.setenv(ENV_TEMPLATE_WITH_HOTEL, with_hotel)
    monkeypatch.setenv(ENV_TEMPLATE_NO_HOTEL, no_hotel)

    with pytest.raises(ReengagementConfigurationError):
        load_reengagement_settings()
    with caplog.at_level("ERROR"):
        assert reengagement_settings_or_none() is None
    assert "reengagement_template_misconfigured" in caplog.text


def test_every_language_has_a_code_and_texts_with_and_without_a_hotel() -> None:
    for language in ("ar", "en", "id"):
        assert language in LANGUAGE_CODES
        named = render_reengagement(_SETTINGS, language=language, hotel_name="H")
        plain = render_reengagement(_SETTINGS, language=language, hotel_name=None)
        assert named.text.count("H") >= 1
        assert "{" not in named.text and "{" not in plain.text
        assert named.language_code == plain.language_code == LANGUAGE_CODES[language]


def test_a_hotel_is_the_one_body_parameter_and_is_in_the_recorded_text() -> None:
    rendered = render_reengagement(_SETTINGS, language="en", hotel_name="Hotel Two")

    assert rendered.template_name == "reengagement_with_hotel"
    assert rendered.body_parameters == ("Hotel Two",)
    assert rendered.text == (
        "We tried to reach you about your request at Hotel Two. Reply to this "
        "message whenever it suits you and we will continue from there."
    )


def test_no_hotel_uses_the_variant_with_no_parameters() -> None:
    rendered = render_reengagement(_SETTINGS, language="id", hotel_name=None)

    assert rendered.template_name == "reengagement_no_hotel"
    assert rendered.body_parameters == ()
    assert rendered.language_code == "id"
    assert "{" not in rendered.text


def test_an_unknown_language_gets_arabic_alone() -> None:
    rendered = render_reengagement(_SETTINGS, language=None, hotel_name=None)

    assert DEFAULT_LANGUAGE == "ar"
    assert rendered.language_code == LANGUAGE_CODES["ar"]
    assert re.search("[ؠ-ي]", rendered.text)
    assert not re.search("[A-Za-z]", rendered.text)


def test_a_hotel_name_is_made_into_one_clean_parameter() -> None:
    assert clean_hotel_name("  Hotel \n\t  Two  ") == "Hotel Two"
    assert clean_hotel_name("   ") is None
    assert clean_hotel_name(None) is None
    assert len(clean_hotel_name("x" * 500) or "") == HOTEL_NAME_MAX_CHARS
