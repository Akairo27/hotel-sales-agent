"""WhatsAppCloudApiSender's own responsibility is narrow: build the right
request and turn a transport-level or API-level failure into
WhatsAppSendError (CLAUDE.md §8 — every external call has explicit
failure handling), mirroring tests/unit/test_llm_client.py's own
docstring for the same reasoning applied to the Gemini transport. No
real network access: httpx.AsyncClient.post is replaced with a stub,
the same failure/success shapes a real call would produce.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from services.agent.whatsapp_send import (
    WhatsAppCloudApiSender,
    WhatsAppSendConfigurationError,
    WhatsAppSendError,
    WhatsAppSendSettings,
    load_whatsapp_send_settings,
)

_SETTINGS = WhatsAppSendSettings(
    phone_number_id="test-phone-number-id",
    access_token="test-access-token",
    timeout_ms=10_000,
)

_REQUEST = httpx.Request("POST", "https://graph.facebook.com/v21.0/test/messages")


def _patch_post(monkeypatch: pytest.MonkeyPatch, response: httpx.Response) -> None:
    async def _post(
        _self: httpx.AsyncClient, _url: str, **_kwargs: Any
    ) -> httpx.Response:
        return response

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)


def _patch_post_to_raise(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    async def _post(
        _self: httpx.AsyncClient, _url: str, **_kwargs: Any
    ) -> httpx.Response:
        raise exc

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)


def test_send_text_returns_the_whatsapp_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = httpx.Response(
        200, request=_REQUEST, json={"messages": [{"id": "wamid.OUTBOUND123"}]}
    )
    _patch_post(monkeypatch, response)
    sender = WhatsAppCloudApiSender(_SETTINGS)

    message_id = asyncio.run(sender.send_text(to_phone="966500000001", body="hello"))

    assert message_id == "wamid.OUTBOUND123"


def test_send_text_wraps_an_error_status_as_whatsapp_send_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = httpx.Response(
        401, request=_REQUEST, json={"error": {"message": "invalid token"}}
    )
    _patch_post(monkeypatch, response)
    sender = WhatsAppCloudApiSender(_SETTINGS)

    with pytest.raises(WhatsAppSendError):
        asyncio.run(sender.send_text(to_phone="966500000001", body="hello"))


def test_send_text_wraps_a_transport_timeout_as_whatsapp_send_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_post_to_raise(monkeypatch, httpx.ReadTimeout("timed out"))
    sender = WhatsAppCloudApiSender(_SETTINGS)

    with pytest.raises(WhatsAppSendError):
        asyncio.run(sender.send_text(to_phone="966500000001", body="hello"))


def test_send_text_raises_when_the_success_response_has_no_message_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 2xx response is not proof of a usable result on its own — the
    caller needs the message id to record it (webhook.py stores it in
    messages.whatsapp_message_id), so a response shaped unexpectedly
    must fail loudly here rather than propagate a missing id silently."""
    response = httpx.Response(200, request=_REQUEST, json={"unexpected": "shape"})
    _patch_post(monkeypatch, response)
    sender = WhatsAppCloudApiSender(_SETTINGS)

    with pytest.raises(WhatsAppSendError):
        asyncio.run(sender.send_text(to_phone="966500000001", body="hello"))


def test_load_whatsapp_send_settings_requires_phone_number_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "token")

    with pytest.raises(
        WhatsAppSendConfigurationError, match="WHATSAPP_PHONE_NUMBER_ID"
    ):
        load_whatsapp_send_settings()


def test_load_whatsapp_send_settings_requires_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "id")
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN", raising=False)

    with pytest.raises(WhatsAppSendConfigurationError, match="WHATSAPP_ACCESS_TOKEN"):
        load_whatsapp_send_settings()


def test_load_whatsapp_send_settings_with_a_valid_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "id")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "token")

    settings = load_whatsapp_send_settings()

    assert settings.phone_number_id == "id"
    assert settings.access_token == "token"
