"""The one place the WhatsApp Cloud API's outbound send endpoint is
called — mirrors services/agent/llm/client.py's pattern (CLAUDE.md §9: a
single interface module, no direct SDK/API calls scattered across the
code) for the same reason: services/agent/webhook.py needs to send a
text message, or a message with reply buttons, without knowing anything
about the Graph API's request shape, and tests need a fake transport with
no network access.

See WHATSAPP_GRAPH_API_VERSION's own comment below for why its pin is
not verified the way client.py's Gemini model pin is.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

# Documented, not empirically verified, unlike the Gemini pin
# (services/agent/llm/config.py's ALLOWED_MODELS was confirmed live
# against a real API key). This is Meta's currently-documented stable
# Graph API version, not something this repo has ever called for real —
# no WhatsApp Business credentials exist here to verify it against.
# Whoever first deploys this against a real account must confirm it is
# still current before trusting this pin the way the Gemini one is
# trusted.
WHATSAPP_GRAPH_API_VERSION = "v21.0"

# A single outbound message is one small JSON POST — generous enough to
# absorb ordinary network jitter without tying up a webhook request
# indefinitely if the Graph API itself hangs (CLAUDE.md §8).
_DEFAULT_TIMEOUT_MS = 10_000

# The Cloud API's limit on text.body, from Meta's "Text messages" reference
# ("Maximum 4096 characters", checked 2026-09-29). services/agent/webhook.py
# compares it with len() -- Unicode code points; whether Meta counts code
# points or UTF-16 units is not stated, so a reply within this limit can in
# principle still be rejected, and that rejection is handled like any other
# failed send.
WHATSAPP_TEXT_BODY_MAX_CHARS = 4096

# Limits of an interactive reply-buttons message, from Meta's "Interactive
# reply buttons messages" reference (checked 2026-10-01): at most 3
# buttons, body text 1024 characters, button title 20, button id 256.
# Counted with len(), as above.
MAX_REPLY_BUTTONS = 3
REPLY_BUTTONS_BODY_MAX_CHARS = 1024
REPLY_BUTTON_TITLE_MAX_CHARS = 20
REPLY_BUTTON_ID_MAX_CHARS = 256


class WhatsAppSendConfigurationError(Exception):
    """Raised when WHATSAPP_PHONE_NUMBER_ID or WHATSAPP_ACCESS_TOKEN is
    unset. Separate from webhook.WebhookConfigurationError, the same way
    WhatsAppSendSettings is separate from WebhookSettings below — a
    different configuration domain."""


class WhatsAppSendError(Exception):
    """Raised when the WhatsApp Cloud API send call fails — a timeout, a
    network error, or the API itself reporting an error (CLAUDE.md §8:
    every external call has a timeout and explicit failure handling).

    The caller is expected to treat this as "the message was not
    delivered" and act accordingly (log loudly, do not retry blindly —
    see webhook.py's module docstring for why retrying this specific
    call is not the same question as whether Meta retries the webhook
    delivery that triggered it).
    """


class InvalidReplyButtonsError(WhatsAppSendError):
    """Raised before any request when a reply-buttons message breaks one of
    Meta's limits (the constants above). The caller treats it like any
    failed send."""


@dataclass(frozen=True)
class ReplyButton:
    """One reply button: the id WhatsApp echoes back when it is tapped,
    and the title the customer sees."""

    button_id: str
    title: str


def check_reply_buttons(body: str, buttons: tuple[ReplyButton, ...]) -> None:
    """Raises InvalidReplyButtonsError unless body and buttons fit Meta's
    limits for a reply-buttons message."""
    if not 1 <= len(buttons) <= MAX_REPLY_BUTTONS:
        raise InvalidReplyButtonsError(f"{len(buttons)} buttons, not 1 to 3")
    if not body.strip() or len(body) > REPLY_BUTTONS_BODY_MAX_CHARS:
        raise InvalidReplyButtonsError(f"body of {len(body)} characters")
    for button in buttons:
        if not 1 <= len(button.title) <= REPLY_BUTTON_TITLE_MAX_CHARS:
            raise InvalidReplyButtonsError(f"title of {len(button.title)} characters")
        if not 1 <= len(button.button_id) <= REPLY_BUTTON_ID_MAX_CHARS:
            raise InvalidReplyButtonsError(
                f"button id of {len(button.button_id)} characters"
            )


@dataclass(frozen=True)
class WhatsAppSendSettings:
    """The two WhatsApp Cloud API values this module needs to send a
    message — separate from webhook.py's WebhookSettings, which holds
    the two values needed to verify an *inbound* delivery. Different
    concerns, loaded independently, same as that module's own docstring
    already reasons about its two secrets.
    """

    phone_number_id: str
    access_token: str
    timeout_ms: int


def load_whatsapp_send_settings() -> WhatsAppSendSettings:
    """Raises: WhatsAppSendConfigurationError if either variable is
    unset."""
    phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
    if not phone_number_id:
        raise WhatsAppSendConfigurationError("WHATSAPP_PHONE_NUMBER_ID is not set")
    access_token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
    if not access_token:
        raise WhatsAppSendConfigurationError("WHATSAPP_ACCESS_TOKEN is not set")
    return WhatsAppSendSettings(
        phone_number_id=phone_number_id,
        access_token=access_token,
        timeout_ms=_DEFAULT_TIMEOUT_MS,
    )


# Markdown's bold/strikethrough double the marker WhatsApp's own syntax
# uses single (**bold** vs *bold*, ~~strike~~ vs ~strike~) -- a customer
# reading a reply with the Markdown form sees literal asterisks/tildes,
# not formatted text. Markdown's italic (_word_) already matches
# WhatsApp's own, so there is nothing to convert there. `.+?` is
# non-greedy so "**a** and **b**" converts as two spans, not one
# swallowing everything between the first "**" and the last.
_MARKDOWN_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MARKDOWN_STRIKETHROUGH_RE = re.compile(r"~~(.+?)~~")


def to_whatsapp_formatting(text: str) -> str:
    """Converts Markdown-style bold and strikethrough spans into
    WhatsApp's own single-marker syntax.

    A deterministic, code-level conversion rather than relying on the
    prompt alone (services/agent/llm/prompt.py's whatsapp_formatting rule
    is a second, best-effort line of defense, not the only one) -- a
    model reply that slips back into Markdown still reaches the customer
    correctly formatted.
    """
    converted = _MARKDOWN_BOLD_RE.sub(r"*\1*", text)
    return _MARKDOWN_STRIKETHROUGH_RE.sub(r"~\1~", converted)


def _graph_api_error_detail(body: object) -> str | None:
    """Extracts only the Graph API's own `error.code`/`error.message`
    fields from a parsed JSON response body, if present — never the full
    body. Every caller of this helper builds a WhatsAppSendError message
    that ends up in a structured log line (services/agent/webhook.py's
    "exception_message") in the same process that just sent a live
    access token in this request's headers, so nothing beyond these two
    identifying fields is safe to include.
    """
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    fields = (("code", error.get("code")), ("message", error.get("message")))
    parts = [f"{name}={value}" for name, value in fields if value is not None]
    return ", ".join(parts) if parts else None


class WhatsAppSender(Protocol):
    """What webhook.py needs from a WhatsApp transport — small enough
    for a test fake to implement with no network access, mirroring
    services/agent/llm/client.py's ModelTransport."""

    async def send_text(
        self, *, to_phone: str, body: str
    ) -> str:  # returns the WhatsApp message id
        ...

    async def send_reply_buttons(
        self, *, to_phone: str, body: str, buttons: tuple[ReplyButton, ...]
    ) -> str:  # returns the WhatsApp message id
        ...


class WhatsAppCloudApiSender:
    """The real transport, over the WhatsApp Cloud API's messages
    endpoint.

    to_phone must be in the wa_id shape the Cloud API expects — digits
    only, no leading "+" (webhook.py's own customer_phone column always
    carries the "+", so the caller strips it — see _normalize_phone's
    docstring for the reverse conversion this mirrors).
    """

    def __init__(self, settings: WhatsAppSendSettings) -> None:
        self._settings = settings
        self._url = (
            f"https://graph.facebook.com/{WHATSAPP_GRAPH_API_VERSION}/"
            f"{settings.phone_number_id}/messages"
        )

    async def send_text(self, *, to_phone: str, body: str) -> str:
        """Sends one text message. Returns the WhatsApp-assigned message
        id for the outbound message (stored in messages.whatsapp_message_id
        by the caller, the same column an inbound message's id occupies).

        Raises:
            WhatsAppSendError: the request timed out, failed at the
                transport level, or the API reported a non-2xx response.
        """
        return await self._post(
            {
                "messaging_product": "whatsapp",
                "to": to_phone,
                "type": "text",
                "text": {"body": body},
            }
        )

    async def send_reply_buttons(
        self, *, to_phone: str, body: str, buttons: tuple[ReplyButton, ...]
    ) -> str:
        """Sends one interactive message with reply buttons, in the shape
        of Meta's "Interactive reply buttons messages" reference: no
        header or footer, the body text, and one "reply" button per entry.
        Returns the WhatsApp-assigned message id, as send_text does.

        Raises:
            InvalidReplyButtonsError: body or buttons break Meta's limits;
                nothing was sent.
            WhatsAppSendError: as for send_text.
        """
        check_reply_buttons(body, buttons)
        return await self._post(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to_phone,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body},
                    "action": {
                        "buttons": [
                            {
                                "type": "reply",
                                "reply": {
                                    "id": button.button_id,
                                    "title": button.title,
                                },
                            }
                            for button in buttons
                        ]
                    },
                },
            }
        )

    async def _post(self, payload: dict[str, Any]) -> str:
        """POSTs one message payload; returns its WhatsApp message id.

        Raises:
            WhatsAppSendError: see send_text.
        """
        headers = {"Authorization": f"Bearer {self._settings.access_token}"}
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.timeout_ms / 1000
            ) as client:
                response = await client.post(self._url, json=payload, headers=headers)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            # Never interpolate str(exc) or the raw response here: this
            # request just carried a live Bearer token in its headers,
            # and this message is the only thing that flows into
            # webhook.py's "exception_message" log field. httpx itself
            # doesn't put headers in its exception text today, but a
            # library upgrade or an edge case in an underlying transport
            # error is not something to rely on for that — only
            # identifying fields we've deliberately chosen are safe.
            response_obj = getattr(exc, "response", None)
            status_code = response_obj.status_code if response_obj is not None else None
            detail = None
            if response_obj is not None:
                try:
                    detail = _graph_api_error_detail(response_obj.json())
                except ValueError:
                    detail = None
            description = f"WhatsApp send failed: {type(exc).__name__}"
            if status_code is not None:
                description += f" (status={status_code}"
                description += f", {detail})" if detail else ")"
            raise WhatsAppSendError(description) from exc

        data = response.json()
        try:
            return str(data["messages"][0]["id"])
        except (KeyError, IndexError, TypeError) as exc:
            detail = _graph_api_error_detail(data)
            description = "WhatsApp send succeeded but response had no message id"
            if detail:
                description += f" ({detail})"
            raise WhatsAppSendError(description) from exc
