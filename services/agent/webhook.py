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
4. Call generate_reply. Its tool-calling loop can raise any of several
   exceptions (see conversation.py's own docstring) after one or more
   real, already-paid-for model calls happened in the same turn — not
   only before any of them, since the per-conversation and daily spend
   caps are now re-checked before every model call, not just the first.
5. Whatever generate_reply raises or returns, record any usage it
   reports before deciding how to respond. See _handle_generate_reply_
   failure below: recording happens exactly once, driven by whether the
   exception is carrying usage (services.agent.llm.errors.
   read_usage_so_far), not by a per-exception-type checklist — a new
   exception type added to generate_reply's loop in the future is
   covered automatically, without touching this module.

This module never lets a 500 escape from generate_reply or
record_token_usage, for one reason that applies uniformly regardless of
which exception fired or why: migration 0024's idempotent message insert
makes any Meta retry resolve as a duplicate before ever reaching
generate_reply or record_token_usage again (see _insert_inbound_message
below) — so a 500 here can never be fixed by the retry it provokes, and
can only turn an already-spent, unrecorded model call into a
*permanently* unrecorded one. Turning every such failure into a 200
instead is not the same as hiding it: everything that is not one of the
two expected, named outcomes ("capped", "usage_unavailable") is logged at
ERROR with the exception's own type name, message, and full traceback
(see _handle_generate_reply_failure) — a real bug or a database outage
stays exactly as visible in the logs as it would be behind a 500, it just
stops provoking a retry that cannot help.
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
from services.agent.llm.conversation import UsageTotals, generate_reply
from services.agent.llm.errors import (
    DailySpendCapExceededError,
    TokenSpendCapExceededError,
    TurnCapExceededError,
    UsageUnavailableError,
    read_usage_so_far,
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


def _record_usage_or_log_failure(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    customer_phone: str,
    usage: UsageTotals,
    now: datetime,
) -> bool:
    """Attempts record_token_usage; on any failure (deliberately not
    narrowed to psycopg.Error — see the call site this was extracted
    from), logs at ERROR with the conversation id, the usage figures,
    the exception's type and message, and a full traceback, instead of
    propagating — then returns False rather than raising. Returns True
    on success.

    Shared by every call site in this module that must not lose usage to
    an unhandled 500 — the module docstring explains why: the normal
    post-reply write, and _handle_generate_reply_failure's write for
    whatever usage a generate_reply exception is carrying. A database
    outage or a genuine bug hitting this specific write must be exactly
    as loud as one hitting the generic exception funnel below — CLAUDE.md
    forbids folding a real fault quietly into a structured-but-empty log
    line.
    """
    try:
        record_token_usage(
            conn,
            conversation_id=conversation_id,
            customer_phone=customer_phone,
            usage=usage,
            now=now,
        )
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "record_token_usage_failed",
                    "conversation_id": conversation_id,
                    "prompt_tokens": usage.prompt_tokens,
                    "candidates_tokens": usage.candidates_tokens,
                    "total_tokens": usage.total_tokens,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )
        return False
    return True


_STATUS_CAPPED = "capped"
_STATUS_USAGE_UNAVAILABLE = "usage_unavailable"
_STATUS_TURN_FAILED = "turn_failed"


def _status_for_generate_reply_error(exc: Exception) -> str:
    """Maps a generate_reply exception to this endpoint's response
    status. Purely a label for the response body and for which extra log
    event (if any) to emit — never a factor in whether usage gets
    recorded, which is unconditional in _handle_generate_reply_failure
    below, driven by whether the exception is carrying usage at all, not
    by its type.

    An exception type not named here still gets its usage recorded and
    still never produces a 500; it only falls into the generic
    _STATUS_TURN_FAILED bucket instead of a specific one — logged loudly
    enough (event, exception type, message, full traceback) to be found
    the moment it happens. The point of always returning 200 is to stop
    Meta's retry-into-a-permanent-gap trap, not to make a real bug quiet.
    """
    if isinstance(
        exc,
        (TurnCapExceededError, TokenSpendCapExceededError, DailySpendCapExceededError),
    ):
        return _STATUS_CAPPED
    if isinstance(exc, UsageUnavailableError):
        return _STATUS_USAGE_UNAVAILABLE
    return _STATUS_TURN_FAILED


def _handle_generate_reply_failure(
    conn: psycopg.Connection[Any],
    exc: Exception,
    *,
    conversation_id: int,
    customer_phone: str,
    now: datetime,
) -> JSONResponse:
    """The single funnel for everything generate_reply can raise.

    Records whatever usage the exception is carrying — present only when
    one or more real model calls already happened this turn before it
    was raised, per errors.read_usage_so_far and conversation.py's own
    docstring — then logs and always returns 200. Adding a new exception
    type to generate_reply's tool-calling loop in the future needs no
    change here: read_usage_so_far works on any exception, and an
    unnamed type simply falls into the loudly-logged _STATUS_TURN_FAILED
    bucket in _status_for_generate_reply_error above.
    """
    usage_so_far = read_usage_so_far(exc)
    if usage_so_far is not None and usage_so_far.total_tokens > 0:
        _record_usage_or_log_failure(
            conn,
            conversation_id=conversation_id,
            customer_phone=customer_phone,
            usage=usage_so_far,
            now=now,
        )

    status = _status_for_generate_reply_error(exc)
    if status == _STATUS_USAGE_UNAVAILABLE:
        logger.error(
            json.dumps(
                {"event": "usage_unavailable", "conversation_id": conversation_id}
            )
        )
    elif status == _STATUS_TURN_FAILED:
        # Deliberately loud: CLAUDE.md forbids folding a real bug or a
        # database outage quietly into a generic bucket. exc_info=exc
        # attaches the full traceback to the log record — the type name
        # and message are also in the JSON body so they are greppable
        # without needing the traceback rendered alongside them.
        logger.error(
            json.dumps(
                {
                    "event": "turn_failed",
                    "conversation_id": conversation_id,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )
    # _STATUS_CAPPED needs no additional log here: TurnCapExceededError
    # and TokenSpendCapExceededError are expected, common outcomes with
    # nothing to add beyond the status itself, and DailySpendCapExceeded-
    # Error already logs its own structured ERROR event inside
    # check_token_spend_caps every time it is raised (services/agent/
    # llm/caps.py) — logging it again here would just duplicate that
    # event under a different name.

    return JSONResponse({"status": status})


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
        except Exception as exc:
            return _handle_generate_reply_failure(
                conn,
                exc,
                conversation_id=conversation_id,
                customer_phone=inbound.customer_phone,
                now=now,
            )

        if _record_usage_or_log_failure(
            conn,
            conversation_id=conversation_id,
            customer_phone=inbound.customer_phone,
            usage=reply.usage,
            now=now,
        ):
            return JSONResponse({"status": "processed"})
        return JSONResponse({"status": "usage_not_recorded"})
