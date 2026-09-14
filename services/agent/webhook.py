"""The WhatsApp Cloud API webhook — ARCHITECTURE.md §2, §7, §8; PLAN.md
phase 4.

Deliberately incomplete: this module verifies the channel, stores the
inbound message, enforces CLAUDE.md §9's two required caps (per-
conversation token spend, per-number-per-day message rate), and calls the
model if neither cap blocks it. It never runs the output guard and never
sends anything back to the customer over the WhatsApp Cloud API — that is
a separate, later PR's job. Splitting it this way lets signature
verification, idempotent storage, and cap enforcement ship and be tested
independently of the (much larger) reply-generation/guard/send pipeline.

Order matters and is deliberate:
1. Verify X-Hub-Signature-256 against the raw body, before touching the
   database at all. An unsigned or forged request leaves no trace.
2. Store the inbound message. A message that later gets capped is not a
   lost message — it stays in the database so a "the bot never replied"
   complaint can be investigated.
3. Check the per-number-per-day message rate (services.agent.llm.caps),
   before generate_reply is even called.
4. Call generate_reply, which enforces the per-conversation token cap
   (and the global daily spend cap) internally before making any model
   call itself.
5. Record the usage generate_reply actually reports. This runs whether or
   not the reply ever reaches a customer: real money was already spent
   the moment the model was called, and a spend cap that only sees usage
   from calls that got all the way to a successful send would not be
   capping anything for as long as the send/guard PR is not yet merged.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from services.agent.llm.caps import (
    MessageRateCapExceededError,
    check_message_rate_cap,
    record_token_usage,
)
from services.agent.llm.client import GeminiTransport, ModelTransport
from services.agent.llm.config import LlmSettings, load_llm_settings
from services.agent.llm.conversation import generate_reply
from services.agent.llm.errors import (
    DailySpendCapExceededError,
    TokenSpendCapExceededError,
    TurnCapExceededError,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_SIGNATURE_HEADER = "x-hub-signature-256"
_SIGNATURE_PREFIX = "sha256="


class WebhookConfigurationError(Exception):
    """Raised when WHATSAPP_VERIFY_TOKEN or WHATSAPP_APP_SECRET is unset.

    A separate exception from services.agent.llm's LlmConfigurationError:
    this is webhook-channel configuration, a different domain from the
    model configuration that error type documents.
    """


@dataclass(frozen=True)
class WebhookSettings:
    """The two WhatsApp Cloud API secrets this module needs. Separate from
    LlmSettings — a different configuration domain, loaded independently."""

    verify_token: str
    app_secret: str


def load_webhook_settings() -> WebhookSettings:
    """Raises: WebhookConfigurationError if either variable is unset."""
    verify_token = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
    if not verify_token:
        raise WebhookConfigurationError("WHATSAPP_VERIFY_TOKEN is not set")
    app_secret = os.environ.get("WHATSAPP_APP_SECRET", "")
    if not app_secret:
        raise WebhookConfigurationError("WHATSAPP_APP_SECRET is not set")
    return WebhookSettings(verify_token=verify_token, app_secret=app_secret)


def get_webhook_settings() -> WebhookSettings:
    """A separate function from load_webhook_settings so tests can
    monkeypatch just this call site, the same pattern as get_llm_settings
    and get_model_transport below."""
    return load_webhook_settings()


def get_llm_settings() -> LlmSettings:
    return load_llm_settings()


def get_model_transport(settings: LlmSettings) -> ModelTransport:
    return GeminiTransport(settings)


@contextlib.contextmanager
def get_db_connection() -> Iterator[psycopg.Connection[Any]]:
    """One connection per request, closed when the request ends.

    Autocommit: each write below (the conversation upsert, the message
    insert, the token_usage insert) is independently meaningful and must
    survive even if a later step in the same request is capped — the same
    reasoning tests/conftest.py's db_conn fixture gives for autocommit.
    No pooling in this PR; that is an operational concern for whichever PR
    first deploys this service, not a correctness concern this one needs
    to solve.
    """
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


def _signature_is_valid(
    *, body: bytes, signature_header: str | None, app_secret: str
) -> bool:
    """Verifies X-Hub-Signature-256 against the raw request body —
    CLAUDE.md §8: verify Meta's signature on every webhook call, reject
    unsigned. Must run before any parsing or database access: a forged or
    replayed request must leave zero trace."""
    if signature_header is None or not signature_header.startswith(_SIGNATURE_PREFIX):
        return False
    provided_digest = signature_header[len(_SIGNATURE_PREFIX) :]
    expected_digest = hmac.new(
        app_secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(provided_digest, expected_digest)


@dataclass(frozen=True)
class InboundMessage:
    customer_phone: str
    customer_name: str | None
    whatsapp_message_id: str
    body: str


def _normalize_phone(wa_id: str) -> str:
    """WhatsApp's wa_id carries no leading '+'; every customer_phone
    column in this schema does (quotes.customer_phone, migration 0008
    onward) — one normalization point so the format never drifts."""
    return wa_id if wa_id.startswith("+") else f"+{wa_id}"


def _parse_inbound_message(payload: dict[str, Any]) -> InboundMessage | None:
    """Extracts the first inbound text message from a WhatsApp Cloud API
    webhook payload, or None for any event this module does not handle —
    delivery/read status callbacks, non-text message types, or a payload
    that does not match the expected shape at all. Meta sends many event
    shapes to the same URL; treating an unrecognized one as a no-op
    (rather than raising) is the correct, expected behavior for a webhook
    consumer that only acts on inbound text messages today.
    """
    try:
        value = payload["entry"][0]["changes"][0]["value"]
        messages = value.get("messages")
        if not messages:
            return None
        message = messages[0]
        if message.get("type") != "text":
            return None
        contacts = value.get("contacts") or []
        wa_id = contacts[0]["wa_id"] if contacts else message["from"]
        customer_name = None
        if contacts:
            customer_name = (contacts[0].get("profile") or {}).get("name")
        return InboundMessage(
            customer_phone=_normalize_phone(wa_id),
            customer_name=customer_name,
            whatsapp_message_id=message["id"],
            body=message["text"]["body"],
        )
    except KeyError, IndexError, TypeError:
        return None


def _find_or_create_conversation(
    conn: psycopg.Connection[Any], *, customer_phone: str
) -> int:
    """Race-safe under concurrent webhook deliveries for the same number:
    migration 0026's UNIQUE (customer_phone) backs this ON CONFLICT, so
    two simultaneous deliveries for one number always resolve to the same
    conversation row instead of racing into two."""
    row = conn.execute(
        "INSERT INTO conversations (customer_phone) VALUES (%s) "
        "ON CONFLICT (customer_phone) "
        "DO UPDATE SET customer_phone = EXCLUDED.customer_phone "
        "RETURNING id",
        (customer_phone,),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "INSERT ... ON CONFLICT DO UPDATE ... RETURNING id returned no row"
        )
    return int(row[0])


def _insert_inbound_message(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    customer_phone: str,
    whatsapp_message_id: str,
    body: str,
) -> int | None:
    """Idempotent: migration 0024's partial unique index on
    whatsapp_message_id makes a duplicate delivery a no-op here, returning
    None instead of inserting a second row — CLAUDE.md's "the same
    WhatsApp message_id processed twice must produce one booking"
    requirement applies at the logging layer too."""
    row = conn.execute(
        "INSERT INTO messages "
        "(conversation_id, customer_phone, direction, whatsapp_message_id, body) "
        "VALUES (%s, %s, 'inbound', %s, %s) "
        "ON CONFLICT (whatsapp_message_id) WHERE whatsapp_message_id IS NOT NULL "
        "DO NOTHING RETURNING id",
        (conversation_id, customer_phone, whatsapp_message_id, body),
    ).fetchone()
    return int(row[0]) if row is not None else None


@router.get("/webhook/whatsapp")
async def verify_subscription(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
) -> PlainTextResponse:
    """Meta's subscription verification handshake.

    Raises:
        HTTPException(403): hub.mode is not "subscribe", or
            hub.verify_token does not match WHATSAPP_VERIFY_TOKEN.
    """
    settings = get_webhook_settings()
    if hub_mode != "subscribe" or not hmac.compare_digest(
        hub_verify_token, settings.verify_token
    ):
        raise HTTPException(status_code=403, detail="verification failed")
    return PlainTextResponse(content=hub_challenge)


@router.post("/webhook/whatsapp")
async def receive_message(request: Request) -> JSONResponse:
    """Processes one WhatsApp Cloud API webhook delivery — see this
    module's own docstring for why it stops where it does.

    Raises:
        HTTPException(401): the signature is missing or does not match
            X-Hub-Signature-256 — checked before any database access.
        HTTPException(400): the signed body is not valid JSON.
    """
    webhook_settings = get_webhook_settings()
    body = await request.body()
    if not _signature_is_valid(
        body=body,
        signature_header=request.headers.get(_SIGNATURE_HEADER),
        app_secret=webhook_settings.app_secret,
    ):
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body") from None

    inbound = _parse_inbound_message(payload)
    if inbound is None:
        return JSONResponse({"status": "ignored"})

    llm_settings = get_llm_settings()
    now = datetime.now(UTC)

    with get_db_connection() as conn:
        conversation_id = _find_or_create_conversation(
            conn, customer_phone=inbound.customer_phone
        )
        message_id = _insert_inbound_message(
            conn,
            conversation_id=conversation_id,
            customer_phone=inbound.customer_phone,
            whatsapp_message_id=inbound.whatsapp_message_id,
            body=inbound.body,
        )
        if message_id is None:
            return JSONResponse({"status": "duplicate"})

        try:
            check_message_rate_cap(
                conn,
                customer_phone=inbound.customer_phone,
                now=now,
                settings=llm_settings,
            )
        except MessageRateCapExceededError:
            logger.info(
                json.dumps(
                    {
                        "event": "message_rate_cap_blocked",
                        "conversation_id": conversation_id,
                    }
                )
            )
            return JSONResponse({"status": "rate_limited"})

        transport = get_model_transport(llm_settings)
        try:
            reply = await generate_reply(
                conn,
                conversation_id=conversation_id,
                customer_name=inbound.customer_name,
                transport=transport,
                settings=llm_settings,
                now=now,
            )
        except (
            TurnCapExceededError,
            TokenSpendCapExceededError,
            DailySpendCapExceededError,
        ):
            return JSONResponse({"status": "capped"})

        record_token_usage(
            conn,
            conversation_id=conversation_id,
            customer_phone=inbound.customer_phone,
            usage=reply.usage,
            now=now,
        )

    return JSONResponse({"status": "processed"})
