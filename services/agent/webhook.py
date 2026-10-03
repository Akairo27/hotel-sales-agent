"""The WhatsApp Cloud API webhook — ARCHITECTURE.md §2, §7, §8; PLAN.md
phase 4.

Verifies the channel, stores the inbound message, enforces CLAUDE.md §9's
message-rate cap, and acks fast — then a FastAPI BackgroundTasks job
enforces the spend/turn caps, calls the model, runs the output guard on
whatever it produced, and sends the result over the WhatsApp Cloud API.
The output guard and the send still ship together, deliberately: a guard
with nothing downstream to protect is untested in the one way that
matters (does a real send ever bypass it), and a send with no guard in
front of it is exactly the "wrong price reaches a customer" failure mode
CLAUDE.md rule 8 exists to prevent. Building either alone would mean
shipping half of a safety property.

Fast-ack / background split (added after a real incident: one turn's
Gemini call took 32s end-to-end after retrying internally, and WhatsApp's
own delivery retried the same message mid-flight because our response
hadn't come back yet — services.agent.llm.client's own retries are the
right fix for the retry itself, but nothing bounds how long a webhook
response can take without this split). Order matters and is deliberate:

1. Verify X-Hub-Signature-256 against the raw body, before touching the
   database at all. An unsigned or forged request leaves no trace beyond
   one webhook_signature_rejected warning (the reason only).
2. Store each inbound message in the delivery -- every one, not just the
   first. A message that later gets capped or blocked is not a lost
   message — it stays in the database so a "the bot never replied"
   complaint can be investigated. Also the dedup boundary: a retried
   WhatsApp delivery for the same whatsapp_message_id short-circuits here
   every time, whether the first delivery is still being generated in the
   background or long since finished. Only a failure before this insert
   answers 500 (so Meta redelivers); every step after it logs and carries
   on, since a redelivery would be dropped as a duplicate. Reactions,
   stickers and unknown types are ignored, not stored
   (_HANDLED_MESSAGE_TYPES).
2a. If a staff member has taken the conversation over from the dashboard
   (services/agent/takeover.py), stop here: the message stays stored for
   them and nothing is sent (owner decision D6, 2026-10-02). The same check
   runs again just before anything goes out to the customer, in
   _deliver_reply and _escalate_and_notify, for a turn the takeover
   overtakes.
3. Check the per-number-per-day message rate (services.agent.llm.caps).
   The first message past it gets the fallback and an escalation; later
   ones that day stay silent (owner decision B).

   Everything through step 3 is synchronous, on one DB connection, and
   fast — confirmed against production timing (comfortably under 1s).
   The response returns here once steps 1-3 pass. A text message is
   answered afterward by _generate_and_deliver_reply (a FastAPI
   BackgroundTasks job) on its own DB connection: the one used above is
   already closed by the time Starlette schedules the background job. A
   voice note, an image or other media never reaches the model: a
   background job sends a fixed notice and escalates
   (_send_notice_without_a_model_turn). Steps 4-7 are the text path; a
   tapped booking button takes it too, and a booking yes that code answers
   (services/agent/booking_yes.py) skips the model call in step 4.

4. Call generate_reply. Its tool-calling loop can raise any of several
   exceptions (see conversation.py's own docstring) after one or more
   real, already-paid-for model calls happened in the same turn — not
   only before any of them, since the per-conversation and daily spend
   caps are re-checked before every model call, not just the first, and
   the model transport itself (services.agent.llm.client) can fail on
   any call after its own retries are exhausted. TurnCapExceededError is
   the one exception that never follows a model call in the same turn —
   it is raised before generate_reply loads even the message window.
5. Whatever generate_reply raises or returns, record any usage it
   reports and increment turn_count before deciding how to respond. See
   _handle_generate_reply_failure below: both writes happen exactly
   once, driven by whether the exception is carrying usage
   (services.agent.llm.errors.read_usage_so_far), not by a
   per-exception-type checklist — a new exception type added to
   generate_reply's loop in the future is covered automatically, without
   touching this module.
5a. EVERY exception from generate_reply -- whatever its type -- is then,
    after step 5's recording, escalated to a human and answered with the
    bilingual fallback message (_escalate_and_notify), with a reason from
    _escalation_reason. CLAUDE.md rule 12: no failed turn ends in
    silence. (A bad tool argument is not a failure at all:
    conversation.py hands the model a fixed tool error and the turn goes
    on.)
6. Run services.agent.output_guard.enforcement.enforce_outbound_text on
   the candidate reply. Allowed: send it. Blocked: never send it, never
   ask the model to rephrase it (a second, unmanipulated attempt is not
   guaranteed, and it would spend more tokens on a turn that already
   failed) — send the fallback (services/agent/fixed_texts.py) instead, a
   fixed, non-LLM-generated text in the customer's language, through the
   exact same enforce_outbound_text
   call (its own module docstring already names this as a canned
   template's intended path, not a bypass). enforce_outbound_text already
   opens the escalation for the blocked reply on its own, so the funnel
   sends the fallback without opening a second one.
7. Send via services.agent.whatsapp_send. A reply that cannot be sent (blank,
   or over the Cloud API's length limit), a guard or send-setup error, and
   a failed send all go to the same funnel as 5a. The funnel attempts the
   escalation and the fallback independently and reports which of the two
   happened (_funnel_status), so a failure is never labelled "escalated"
   when the customer got nothing; a half that failed because the turn's
   connection died is retried once on a fresh connection. What no code can
   fix is listed as residuals in ARCHITECTURE.md §7 ("لا صمت"): the
   database being unavailable (rule 8 sends even the fallback through the
   guard, which reads the database), WhatsApp refusing every send, and a
   hard kill of the process.

Background-job reliability: FastAPI's BackgroundTasks runs as part of
the same ASGI call Starlette is already handling — after the response is
sent, but before the app's callable returns — so a graceful `systemctl
restart` (SIGTERM) waits for an in-flight background job exactly the way
it waits for any other in-flight request, bounded by systemd's
TimeoutStopSec (120s, set explicitly in ops/hotel-agent.service —
comfortable margin over generate_reply's own TURN_BUDGET_SECONDS cap of
75s, plus time for the guard check, the DB writes and the send that
follow a model call). A hard kill (SIGKILL, OOM, crash) loses
whatever was in flight — the same exposure a synchronous in-flight
request already had, not a new regression, but not newly solved by this
split either. See _generate_and_deliver_reply's own docstring for the
one new safety net this split does add: an outermost catch that still
escalates and notifies on literally anything unexpected, since there is
no HTTP response left to carry a failure back to Meta once the ack has
already been sent.

This module never lets a 500 escape from generate_reply, the output
guard, the send, or any of the three usage/message-recording writes, for
one reason that applies uniformly regardless of which step failed or why:
migration 0024's idempotent message insert makes any Meta retry resolve
as a duplicate before ever reaching generate_reply or any of these writes
again (see _insert_inbound_message below) — so a 500 here can never be
fixed by the retry it provokes, and can only turn an already-spent,
unrecorded, or undelivered turn into a *permanently* unrecorded or
undelivered one. Turning every such failure into a 200 instead is not the
same as hiding it: everything that is not one of the expected, named
outcomes is logged at ERROR with the exception's type name and traceback
— a real bug or a database outage stays exactly as visible in the logs as
it would be behind a 500, it just stops provoking a retry that cannot
help. For a generate_reply failure the traceback is frames-only and the
message is left out (_safe_exception_log_fields): a services.pricing
error's message holds cost and floor values, which CLAUDE.md §8 keeps out
of every log.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import traceback
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from services.agent.booking_buttons import (
    BookingOfferButtons,
    ButtonTap,
    booking_offer_buttons,
    buttons_for_reply,
    text_with_button_titles,
)
from services.agent.booking_confirmation import render_booking_passed_on
from services.agent.booking_yes import (
    BookingDecision,
    ButtonMismatch,
    CustomerMessage,
    OfferNewerPrice,
    PassOn,
    confirmation_for_passed_on_quote,
    decide_booking_yes,
)
from services.agent.fixed_texts import (
    FALLBACK,
    NEWER_PRICE,
    PLEASE_TYPE,
    FixedText,
    Language,
    customer_language,
    media_placeholder,
)
from services.agent.llm.booking_follow_up import (
    REQUEST_BOOKING_FOLLOW_UP_TOOL,
    pass_quote_on,
)
from services.agent.llm.caps import (
    MessageRateCapExceededError,
    check_message_rate_cap,
    find_todays_cap_escalation,
    increment_turn_count,
    record_token_usage,
)
from services.agent.llm.client import (
    GeminiTransport,
    ModelTransport,
    OpenRouterTransport,
)
from services.agent.llm.config import LlmSettings, load_llm_settings
from services.agent.llm.conversation import AgentReply, UsageTotals, generate_reply
from services.agent.llm.errors import (
    DailySpendCapExceededError,
    ModelUnavailableError,
    NumberDailyTokenCapExceededError,
    TokenSpendCapExceededError,
    ToolLoopLimitError,
    TurnBudgetExceededError,
    TurnCapExceededError,
    UnknownToolError,
    UsageUnavailableError,
    read_usage_so_far,
)
from services.agent.llm.session import (
    start_new_session_if_idle,
    touch_last_message_at,
)
from services.agent.output_guard.enforcement import (
    GuardVerdict,
    enforce_outbound_text,
    open_escalation,
)
from services.agent.output_guard.staff_replies import StaffReplyInspection
from services.agent.reengagement_template import RenderedReengagement
from services.agent.staff_follow_up import open_follow_up_for_dates_not_open
from services.agent.takeover import is_taken_over
from services.agent.whatsapp_send import (
    WHATSAPP_TEXT_BODY_MAX_CHARS,
    WhatsAppCloudApiSender,
    WhatsAppMessageRejectedError,
    WhatsAppSender,
    WhatsAppSendSettings,
    load_whatsapp_send_settings,
    to_whatsapp_formatting,
)
from services.pricing.errors import PricingError

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
    route = settings.openrouter_route
    if route is None:
        return GeminiTransport(settings)
    return OpenRouterTransport(
        model=settings.model,
        api_key=settings.api_key,
        providers=route.providers,
        timeout_ms=settings.timeout_ms,
        reasoning_effort=route.reasoning_effort,
    )


def get_whatsapp_send_settings() -> WhatsAppSendSettings:
    return load_whatsapp_send_settings()


def get_whatsapp_sender(settings: WhatsAppSendSettings) -> WhatsAppSender:
    return WhatsAppCloudApiSender(settings)


@contextlib.contextmanager
def get_db_connection() -> Iterator[psycopg.Connection[Any]]:
    """One connection per caller, closed when that caller is done with it.

    Autocommit: each write below (the conversation upsert, the message
    insert, the token_usage insert) is independently meaningful and must
    survive even if a later step is capped — the same reasoning
    tests/conftest.py's db_conn fixture gives for autocommit. No pooling
    in this PR; that is an operational concern for whichever PR first
    deploys this service, not a correctness concern this one needs to
    solve.

    Called twice per turn since the fast-ack/background split: once for
    receive_message's own fast path, and once more inside
    _generate_and_deliver_reply's background job, which cannot reuse the
    first connection — it is already closed by the time Starlette
    schedules that job.
    """
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


def _signature_problem(
    *, body: bytes, signature_header: str | None, app_secret: str
) -> str | None:
    """Why X-Hub-Signature-256 fails to verify against the raw request
    body -- "missing", "malformed" (no sha256= prefix) or "mismatch" -- or
    None when it verifies. CLAUDE.md §8: verify Meta's signature on every
    webhook call, reject unsigned. Must run before any parsing or
    database access: a forged or replayed request leaves no trace beyond
    the one warning receive_message logs, which carries this label and
    never the header's value or the secret."""
    if signature_header is None:
        return "missing"
    if not signature_header.startswith(_SIGNATURE_PREFIX):
        return "malformed"
    provided_digest = signature_header[len(_SIGNATURE_PREFIX) :]
    expected_digest = hmac.new(
        app_secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(provided_digest, expected_digest):
        return "mismatch"
    return None


# How the fast path handles each inbound message type (owner decision A,
# revised 2026-09-29). Only text and a tapped reply button reach a turn
# (_TURN_MESSAGE_TYPES). A voice note or an image gets
# fixed_texts.PLEASE_TYPE; the other media listed get the fallback; both
# open an escalation for staff. Every other type -- reaction, sticker, and
# any type not listed here -- is deliberately ignored: not stored, not
# answered (ARCHITECTURE.md §7, "لا صمت").
_TEXT_MESSAGE_TYPE = "text"
# Not a WhatsApp type: an "interactive" message whose interactive.type is
# "button_reply" (a booking offer's button, services/agent/booking_buttons.py)
# is given this one when parsed. Any other interactive message keeps
# "interactive" and gets the fallback.
_BUTTON_REPLY_MESSAGE_TYPE = "button_reply"
_INTERACTIVE_MESSAGE_TYPE = "interactive"
_TURN_MESSAGE_TYPES = frozenset({_TEXT_MESSAGE_TYPE, _BUTTON_REPLY_MESSAGE_TYPE})
_PLEASE_TYPE_MESSAGE_TYPES = frozenset({"audio", "image"})
_FALLBACK_MESSAGE_TYPES = frozenset(
    {"video", "document", "location", "contacts", _INTERACTIVE_MESSAGE_TYPE}
)
_HANDLED_MESSAGE_TYPES = (
    _TURN_MESSAGE_TYPES | _PLEASE_TYPE_MESSAGE_TYPES | _FALLBACK_MESSAGE_TYPES
)

_REASON_UNSUPPORTED_MESSAGE_TYPE = "unsupported_message_type"
_REASON_MESSAGE_RATE_CAP_EXCEEDED = "message_rate_cap_exceeded"
_REASON_BOOKING_BUTTON_MISMATCH = "booking_button_mismatch"


@dataclass(frozen=True)
class InboundMessage:
    customer_phone: str
    customer_name: str | None
    whatsapp_message_id: str
    message_type: str
    # The text for a text message and a tapped button's title for a button
    # reply; for any other type a fixed placeholder ("[image message]"),
    # plus its caption when it has one -- what staff and the model's later
    # context see in place of the media itself.
    body: str
    button: ButtonTap | None = None


def _normalize_phone(wa_id: str) -> str:
    """WhatsApp's wa_id carries no leading '+'; every customer_phone
    column in this schema does (quotes.customer_phone, migration 0008
    onward) — one normalization point so the format never drifts."""
    return wa_id if wa_id.startswith("+") else f"+{wa_id}"


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _message_body(message: dict[str, Any], message_type: str) -> str:
    """The text of a text message; for any other type the placeholder
    "[<type> message]", followed by the media's caption when it has one."""
    if message_type == _TEXT_MESSAGE_TYPE:
        text = _as_dict(message.get("text")).get("body")
        if not isinstance(text, str):
            raise KeyError("text message without text.body")
        return text
    placeholder = media_placeholder(message_type)
    caption = _as_dict(message.get(message_type)).get("caption")
    return f"{placeholder} {caption}" if isinstance(caption, str) else placeholder


def _button_tap(message: dict[str, Any]) -> ButtonTap | None:
    """The tapped reply button in an interactive message, or None for any
    other interactive message (a list reply, or a button reply without an
    id or a non-blank title), which then gets the fallback."""
    interactive = _as_dict(message.get(_INTERACTIVE_MESSAGE_TYPE))
    if interactive.get("type") != _BUTTON_REPLY_MESSAGE_TYPE:
        return None
    reply = _as_dict(interactive.get(_BUTTON_REPLY_MESSAGE_TYPE))
    button_id, title = reply.get("id"), reply.get("title")
    if not (isinstance(button_id, str) and isinstance(title, str) and title.strip()):
        return None
    context_id = _as_dict(message.get("context")).get("id")
    return ButtonTap(
        button_id=button_id,
        title=title,
        context_message_id=context_id if isinstance(context_id, str) else None,
    )


def _parse_one_message(
    message: dict[str, Any], contacts: list[Any]
) -> InboundMessage | None:
    """One entry of a webhook's messages array, or None if it lacks what
    every message needs (a sender, an id, a type, and text.body for a text
    message). The sender's display name comes from the contact whose
    wa_id matches; a payload with no "from" falls back to its only
    contact. A tapped reply button becomes a button_reply whose body is
    the button's title."""
    names = {
        contact.get("wa_id"): _as_dict(contact.get("profile")).get("name")
        for contact in map(_as_dict, contacts)
    }
    wa_id = message.get("from")
    if not isinstance(wa_id, str) and len(contacts) == 1:
        wa_id = _as_dict(contacts[0]).get("wa_id")
    message_id = message.get("id")
    message_type = message.get("type")
    if not (
        isinstance(wa_id, str)
        and isinstance(message_id, str)
        and isinstance(message_type, str)
    ):
        return None
    button = _button_tap(message) if message_type == _INTERACTIVE_MESSAGE_TYPE else None
    if button is not None:
        message_type, body = _BUTTON_REPLY_MESSAGE_TYPE, button.title
    else:
        try:
            body = _message_body(message, message_type)
        except KeyError:
            return None
    name = names.get(wa_id)
    return InboundMessage(
        customer_phone=_normalize_phone(wa_id),
        customer_name=name if isinstance(name, str) else None,
        whatsapp_message_id=message_id,
        message_type=message_type,
        body=body,
        button=button,
    )


def _parse_inbound_messages(payload: dict[str, Any]) -> list[InboundMessage]:
    """Every inbound message in a WhatsApp Cloud API webhook payload, of
    any type, across every entry and change -- not just the first (a
    delivery can batch several). Delivery/read status callbacks carry no
    messages and yield nothing; a message missing what it needs is
    skipped, not fatal to the rest. Which types are answered is
    receive_message's decision (_HANDLED_MESSAGE_TYPES), not this
    function's."""
    parsed: list[InboundMessage] = []
    for entry in map(_as_dict, _as_list(payload.get("entry"))):
        for change in map(_as_dict, _as_list(entry.get("changes"))):
            value = _as_dict(change.get("value"))
            contacts = _as_list(value.get("contacts"))
            for message in map(_as_dict, _as_list(value.get("messages"))):
                inbound = _parse_one_message(message, contacts)
                if inbound is not None:
                    parsed.append(inbound)
    return parsed


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


def _insert_outbound_message(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    customer_phone: str,
    whatsapp_message_id: str,
    body: str,
    staff_reply_id: int | None = None,
) -> None:
    """Records one outbound message, linked to the staff reply it carries
    when staff_reply_id is given (migration 0035). Called only after a real WhatsApp
    Cloud API send actually succeeded (see _send_or_log_failure) — a row
    here is always proof of an attempted delivery that got a message id
    back, never merely an intention to send. No idempotency handling
    needed, unlike _insert_inbound_message: each send produces a fresh
    WhatsApp-assigned id, and this function is only ever reached once per
    inbound delivery (a retried inbound delivery short-circuits on
    _insert_inbound_message's own idempotent insert, long before this
    point — see the module docstring).
    """
    if staff_reply_id is None:
        conn.execute(
            "INSERT INTO messages "
            "(conversation_id, customer_phone, direction, whatsapp_message_id, body) "
            "VALUES (%s, %s, 'outbound', %s, %s)",
            (conversation_id, customer_phone, whatsapp_message_id, body),
        )
    else:
        conn.execute(
            "INSERT INTO messages (conversation_id, customer_phone, direction, "
            "whatsapp_message_id, body, staff_reply_id) "
            "VALUES (%s, %s, 'outbound', %s, %s, %s)",
            (
                conversation_id,
                customer_phone,
                whatsapp_message_id,
                body,
                staff_reply_id,
            ),
        )
    touch_last_message_at(conn, conversation_id=conversation_id)


async def _send_text_or_offer(
    sender: WhatsAppSender,
    *,
    conversation_id: int,
    to_phone: str,
    text: str,
    offer: BookingOfferButtons | None,
) -> str:
    """Sends text with offer's reply buttons, or as plain text when there
    are none. Returns the WhatsApp message id.

    Only a definite refusal of the button message as invalid
    (WhatsAppMessageRejectedError: nothing was sent) is logged at ERROR and
    sent once more as plain text with the same body -- the offer question is
    in the body, so a typed yes still works. Any other failure, such as a
    timeout, may have delivered the offer already, so it is raised, never
    retried: the customer must never get the offer twice (owner decisions
    2026-10-01); the caller's failure funnel answers instead.

    Raises:
        Whatever sender.send_reply_buttons raises other than
        WhatsAppMessageRejectedError, or whatever sender.send_text raises.
    """
    if offer is not None:
        try:
            whatsapp_message_id = await sender.send_reply_buttons(
                to_phone=to_phone, body=text, buttons=offer.buttons
            )
        except WhatsAppMessageRejectedError as exc:
            logger.error(
                json.dumps(
                    {
                        "event": "booking_offer_buttons_rejected",
                        "conversation_id": conversation_id,
                        "quote_id": offer.quote_id,
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                    }
                ),
                exc_info=exc,
            )
        else:
            logger.info(
                json.dumps(
                    {
                        "event": "booking_offer_buttons_sent",
                        "conversation_id": conversation_id,
                        "quote_id": offer.quote_id,
                        "whatsapp_message_id": whatsapp_message_id,
                    }
                )
            )
            return whatsapp_message_id
    return await sender.send_text(to_phone=to_phone, body=text)


async def _send_or_log_failure(
    sender: WhatsAppSender,
    *,
    conn: psycopg.Connection[Any],
    conversation_id: int,
    customer_phone: str,
    text: str,
    offer: BookingOfferButtons | None = None,
    staff_reply_id: int | None = None,
) -> str | None:
    """Attempts the real WhatsApp send (_send_text_or_offer); on any
    failure (deliberately not narrowed to WhatsAppSendError — see
    _record_usage_or_log_failure's own docstring for the identical
    reasoning), logs at ERROR with the
    conversation id, exception type, message, and full traceback, and
    returns None rather than propagating. On success, attempts to record
    the outbound message (_insert_outbound_message) and always returns
    the WhatsApp-assigned message id regardless of whether that recording
    succeeded: the send itself already happened — the customer already
    has the message — so a failure to log it afterward must not be
    reported the same way as the send itself failing, and must not
    propagate either, for the same reasons as every other write in this
    module. This function therefore never raises. Only text is recorded,
    never the button titles, so the model's later history reads as for any
    reply.
    """
    to_phone = customer_phone.removeprefix("+")
    try:
        whatsapp_message_id = await _send_text_or_offer(
            sender,
            conversation_id=conversation_id,
            to_phone=to_phone,
            text=text,
            offer=offer,
        )
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "whatsapp_send_failed",
                    "conversation_id": conversation_id,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )
        return None

    try:
        _insert_outbound_message(
            conn,
            conversation_id=conversation_id,
            customer_phone=customer_phone,
            whatsapp_message_id=whatsapp_message_id,
            body=text,
            staff_reply_id=staff_reply_id,
        )
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "outbound_message_not_recorded",
                    "conversation_id": conversation_id,
                    "whatsapp_message_id": whatsapp_message_id,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )
    return whatsapp_message_id


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


def _increment_turn_count_or_log_failure(
    conn: psycopg.Connection[Any], *, conversation_id: int
) -> None:
    """Attempts increment_turn_count; on any failure, logs at ERROR with
    the conversation id and the exception's type/message/traceback
    instead of propagating. Called at the same two points as
    _record_usage_or_log_failure above, under the same condition — see
    caps.increment_turn_count's own docstring for why. No return value:
    unlike usage recording, nothing downstream branches on whether this
    particular write succeeded.
    """
    try:
        increment_turn_count(conn, conversation_id=conversation_id)
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "increment_turn_count_failed",
                    "conversation_id": conversation_id,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )


_STATUS_ACCEPTED = "accepted"
_STATUS_PROCESSED = "processed"

# Per-message statuses in receive_message's response (Meta ignores the
# body; these are for logs and tests).
_STATUS_IGNORED = "ignored"
_STATUS_DUPLICATE = "duplicate"
_STATUS_RATE_LIMITED = "rate_limited"
_STATUS_UNSUPPORTED_TYPE = "unsupported_type"
_STATUS_NOT_STORED = "not_stored"
_STATUS_BATCH = "batch"
# Stored for the staff member who took the conversation over; nothing sent.
_STATUS_TAKEN_OVER = "taken_over"
# A turn whose reply or fallback was not sent because a takeover began while
# it ran (the approved takeover silence, ARCHITECTURE.md §7).
_STATUS_SUPPRESSED_TAKEN_OVER = "suppressed_taken_over"

# The four outcomes of _escalate_and_notify, one per combination of "was a
# human escalation opened" and "did the customer get the fallback". Named
# honestly on purpose: before CLAUDE.md rule 12, a failed fallback send
# still reported "escalated", hiding a silent customer behind a
# success-sounding status.
_STATUS_ESCALATED = "escalated"
_STATUS_ESCALATED_UNDELIVERED = "escalated_undelivered"
_STATUS_NOTIFIED_NO_ESCALATION = "notified_no_escalation"
_STATUS_FAILED_UNRECORDED = "failed_unrecorded"

# The six anticipated stops -- CLAUDE.md §9's four caps, a transport
# failure after client.py's own retries (ModelUnavailableError), and the
# turn's time budget running out (TurnBudgetExceededError). Their messages
# are vetted: they carry counts, USD spend estimates, or an HTTP status,
# never hotel cost or a price floor, so they alone may be stored in
# escalations.notes. Every other exception is recorded by type only (see
# _escalation_notes).
_ANTICIPATED_STOPS: tuple[type[Exception], ...] = (
    TurnCapExceededError,
    TokenSpendCapExceededError,
    NumberDailyTokenCapExceededError,
    DailySpendCapExceededError,
    ModelUnavailableError,
    TurnBudgetExceededError,
)

# One exception type, one reason -- a human reading escalations must be
# able to tell what actually stopped the conversation without
# re-deriving it from notes. Matched with isinstance, in order, so a
# subclass of any of these gets its parent's reason rather than falling
# through to internal_error.
_ESCALATION_REASONS: dict[type[Exception], str] = {
    TurnCapExceededError: "turn_cap_exceeded",
    TokenSpendCapExceededError: "token_spend_cap_exceeded",
    NumberDailyTokenCapExceededError: "number_daily_token_cap_exceeded",
    DailySpendCapExceededError: "daily_spend_cap_exceeded",
    ModelUnavailableError: "model_unavailable",
    TurnBudgetExceededError: "turn_budget_exceeded",
    UsageUnavailableError: "usage_unavailable",
    ToolLoopLimitError: "tool_loop_limit_exceeded",
    UnknownToolError: "unknown_tool",
}
# CLAUDE.md §9's four caps, whose escalations collapse to one per number
# per Asia/Riyadh day (owner decision 2026-09-30, ARCHITECTURE.md §7): a
# number blocked again that day by any of them gets the fallback only,
# and staff work from the escalation already opened.
_COLLAPSED_CAP_STOPS: tuple[type[Exception], ...] = (
    TurnCapExceededError,
    TokenSpendCapExceededError,
    NumberDailyTokenCapExceededError,
    DailySpendCapExceededError,
)
_COLLAPSED_CAP_REASONS: tuple[str, ...] = tuple(
    _ESCALATION_REASONS[stop] for stop in _COLLAPSED_CAP_STOPS
)
# Every services.pricing exception compute_quote lets propagate -- a
# price_rules misconfiguration, or the occupancy-1.0 band gap -- shares
# one reason; the exception type in escalations.notes says which.
_REASON_PRICING_ERROR = "pricing_error"
# Not keyed off any exception class: the model produced a reply that
# cannot be sent (blank, or longer than WhatsApp accepts), the guard or
# send setup raised, or the send itself failed.
_REASON_EMPTY_REPLY = "empty_reply"
_REASON_REPLY_TOO_LONG = "reply_too_long"
_REASON_DELIVERY_FAILED = "delivery_failed"
# A log label only, never stored: the output guard opens (and names) its
# own escalation for a blocked reply before the fallback is sent.
_REASON_OUTPUT_GUARD_BLOCKED = "output_guard_blocked"
# The quote validity the guard applies to a fixed text: none. Every
# fixed_texts rendering is digit-free, so no quote may ever be what makes
# one allowed, and this path must not depend on settings that might be the
# very thing that failed to load.
_FIXED_TEXT_QUOTE_VALIDITY = timedelta(0)
# Anything else -- a bug, a database error inside the turn -- deliberately
# left as one open category, the same way migration 0024's comment on
# escalations.reason leaves the full set of reasons open.
_REASON_INTERNAL_ERROR = "internal_error"


def _escalation_reason(exc: Exception) -> str:
    """The escalations.reason for a generate_reply exception: its entry in
    _ESCALATION_REASONS, pricing_error for any services.pricing exception,
    otherwise internal_error. Never raises -- every exception maps to a
    reason, so no exception type can leave a turn without an escalation."""
    for exception_type, reason in _ESCALATION_REASONS.items():
        if isinstance(exc, exception_type):
            return reason
    if isinstance(exc, PricingError):
        return _REASON_PRICING_ERROR
    return _REASON_INTERNAL_ERROR


def _escalation_notes(exc: Exception | None) -> dict[str, Any]:
    """escalations.notes for a failed turn: always the exception's type,
    and its message only for the vetted _ANTICIPATED_STOPS. A
    services.pricing message can carry cost_per_night, the margin and the
    price floor (compute.py's InconsistentPriceConfigurationError), and
    notes are read by humans and back-office tooling -- CLAUDE.md §8 keeps
    cost out of anything logged or stored for inspection outside the
    pricing audit trail."""
    if exc is None:
        return {}
    notes: dict[str, Any] = {"exception_type": type(exc).__name__}
    if isinstance(exc, _ANTICIPATED_STOPS):
        notes["detail"] = str(exc)
    return notes


def _safe_exception_log_fields(exc: BaseException) -> dict[str, str]:
    """The exception's type and a frames-only traceback, for an exception
    whose message is not vetted (see _ANTICIPATED_STOPS). Deliberately not
    exc_info=exc on the log record: the standard traceback rendering ends
    with the exception's own message, and a services.pricing message
    carries cost and floor values (CLAUDE.md §8: never log cost).
    traceback.format_tb renders file, line, function and source line only."""
    return {
        "exception_type": type(exc).__name__,
        "traceback": "".join(traceback.format_tb(exc.__traceback__)),
    }


def _send_suppressed_by_takeover(
    conn: psycopg.Connection[Any], *, conversation_id: int, withheld: str
) -> bool:
    """Whether a takeover began after this turn started, so `withheld` (a
    log label: "reply" or "fallback") must not be sent -- logged at INFO
    when so. A failed check reads as no takeover (takeover.is_taken_over).
    Never raises."""
    if not is_taken_over(conn, conversation_id=conversation_id):
        return False
    logger.info(
        json.dumps(
            {
                "event": "customer_send_suppressed_taken_over",
                "conversation_id": conversation_id,
                "withheld": withheld,
            }
        )
    )
    return True


def _funnel_status(*, escalated: bool, delivered: bool) -> str:
    if escalated:
        return _STATUS_ESCALATED if delivered else _STATUS_ESCALATED_UNDELIVERED
    return _STATUS_NOTIFIED_NO_ESCALATION if delivered else _STATUS_FAILED_UNRECORDED


def _open_escalation_or_log_failure(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    reason: str,
    exc: Exception | None,
    extra_notes: dict[str, str] | None = None,
) -> int | None:
    """Opens the escalation for a failed turn and logs it; on any failure
    logs conversation_escalation_failed and returns None instead of
    raising, so the fallback send that follows is still attempted.
    extra_notes (fixed, code-chosen values such as a message type -- never
    customer text) are added to _escalation_notes(exc).

    exception_message appears in the conversation_escalated event only for
    ModelUnavailableError: client.py's message carries just the exception
    name plus the provider's HTTP code/status. The cap errors' messages
    hold USD spend estimates and every other message is unvetted, so the
    event carries their type only.
    """
    try:
        escalation_id = open_escalation(
            conn,
            conversation_id=conversation_id,
            reason=reason,
            notes={**_escalation_notes(exc), **(extra_notes or {})},
        )
    except Exception as open_exc:
        logger.error(
            json.dumps(
                {
                    "event": "conversation_escalation_failed",
                    "conversation_id": conversation_id,
                    "reason": reason,
                    "exception_type": type(open_exc).__name__,
                    "exception_message": str(open_exc),
                }
            ),
            exc_info=open_exc,
        )
        return None
    log_fields: dict[str, Any] = {
        "event": "conversation_escalated",
        "conversation_id": conversation_id,
        "reason": reason,
        "escalation_id": escalation_id,
        "exception_type": type(exc).__name__ if exc is not None else None,
    }
    if isinstance(exc, ModelUnavailableError):
        log_fields["exception_message"] = str(exc)
    logger.error(json.dumps(log_fields))
    return escalation_id


def _notice_language(
    conn: psycopg.Connection[Any], conversation_id: int
) -> Language | None:
    """The customer's language for a fixed text, or None -- the bilingual
    rendering -- when it is unknown or cannot be read. A failed read is
    logged as a warning and never blocks the notice itself."""
    try:
        return customer_language(conn, conversation_id)
    except Exception as exc:
        logger.warning(
            json.dumps(
                {
                    "event": "notice_language_lookup_failed",
                    "conversation_id": conversation_id,
                    "exception_type": type(exc).__name__,
                }
            )
        )
        return None


async def _send_fallback_or_log_failure(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    customer_phone: str,
    reason: str,
    escalation_id: int | None,
    notice: FixedText = FALLBACK,
) -> bool:
    """Sends `notice` -- fixed_texts.FALLBACK unless a caller passes
    another fixed text such as fixed_texts.PLEASE_TYPE -- in the customer's
    language (_notice_language), through the output guard like any other
    outbound text (CLAUDE.md rule 8 -- the guard is never bypassed, even
    for a fixed string). Returns whether it was delivered; never raises.

    A blocked notice is structurally impossible -- no rendering of a fixed
    text contains a digit, and tests/integration/test_output_guard.py runs
    every rendering through the guard to prove it -- and is not routed
    around with a second attempt, the infinite-regress trap this design
    avoids. It is logged loudly if it ever happens.
    """
    notice_text = notice.render(_notice_language(conn, conversation_id))
    try:
        verdict = enforce_outbound_text(
            conn,
            conversation_id=conversation_id,
            text=notice_text,
            quote_validity=_FIXED_TEXT_QUOTE_VALIDITY,
        )
        if not verdict.allowed:
            logger.error(
                json.dumps(
                    {
                        "event": "conversation_escalation_fallback_blocked",
                        "conversation_id": conversation_id,
                        "reason": reason,
                        "escalation_id": escalation_id,
                        "fallback_escalation_id": verdict.escalation_id,
                    }
                )
            )
            return False
        sender = get_whatsapp_sender(get_whatsapp_send_settings())
    except Exception as setup_exc:
        logger.error(
            json.dumps(
                {
                    "event": "fallback_delivery_failed",
                    "conversation_id": conversation_id,
                    "reason": reason,
                    "escalation_id": escalation_id,
                    "exception_type": type(setup_exc).__name__,
                    "exception_message": str(setup_exc),
                }
            ),
            exc_info=setup_exc,
        )
        return False
    whatsapp_message_id = await _send_or_log_failure(
        sender,
        conn=conn,
        conversation_id=conversation_id,
        customer_phone=customer_phone,
        text=notice_text,
    )
    return whatsapp_message_id is not None


async def send_fixed_text_or_log_failure(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    customer_phone: str,
    notice: FixedText,
    purpose: str,
) -> bool:
    """Sends one fixed text outside any turn -- today only the takeover
    acknowledgement (services/agent/takeover_ack.py) -- exactly as the
    funnel sends its fallback: in the customer's language, through the
    output guard (CLAUDE.md rule 8), recorded as an outbound message.
    `purpose` labels the log lines. Deliberately no takeover check: the
    acknowledgement is the one text sent during a takeover. Returns whether
    it was delivered; never raises."""
    return await _send_fallback_or_log_failure(
        conn,
        conversation_id=conversation_id,
        customer_phone=customer_phone,
        reason=purpose,
        escalation_id=None,
        notice=notice,
    )


def _sender_for_staff_reply_or_log_failure(
    *, conversation_id: int, staff_reply_id: int
) -> WhatsAppSender | None:
    """The WhatsApp transport for a staff reply, or None -- logged at ERROR
    as staff_reply_delivery_failed -- when it cannot be set up."""
    try:
        return get_whatsapp_sender(get_whatsapp_send_settings())
    except Exception as setup_exc:
        logger.error(
            json.dumps(
                {
                    "event": "staff_reply_delivery_failed",
                    "conversation_id": conversation_id,
                    "staff_reply_id": staff_reply_id,
                    "exception_type": type(setup_exc).__name__,
                    "exception_message": str(setup_exc),
                }
            ),
            exc_info=setup_exc,
        )
        return None


async def send_staff_reply_or_log_failure(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    customer_phone: str,
    staff_reply_id: int,
    inspection: StaffReplyInspection,
) -> bool:
    """Sends one staff reply (services/agent/staff_reply.py) and records it
    as an outbound message linked to its staff_replies row. It sends
    inspection.text and nothing else: only text that went through the
    output guard's staff-reply mode can be sent this way (CLAUDE.md rule
    8). Deliberately no takeover check: the caller claimed the reply while
    its takeover was active. Returns whether it was delivered; never
    raises."""
    sender = _sender_for_staff_reply_or_log_failure(
        conversation_id=conversation_id, staff_reply_id=staff_reply_id
    )
    if sender is None:
        return False
    whatsapp_message_id = await _send_or_log_failure(
        sender,
        conn=conn,
        conversation_id=conversation_id,
        customer_phone=customer_phone,
        text=inspection.text,
        staff_reply_id=staff_reply_id,
    )
    return whatsapp_message_id is not None


async def send_staff_template_or_log_failure(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    customer_phone: str,
    staff_reply_id: int,
    inspection: StaffReplyInspection,
    template: RenderedReengagement,
) -> bool:
    """Sends the re-engagement template for a staff reply
    (services/agent/reengagement_template.py) and records what the customer
    was sent -- the template's rendered text, which is inspection.text -- as
    an outbound message linked to the staff_replies row. As for a text
    reply, only text that went through the output guard's staff-reply mode
    is recorded as sent (CLAUDE.md rule 8): a template whose text is not the
    inspected text is refused and logged. Returns whether it was delivered;
    never raises."""
    if inspection.text != template.text:
        logger.error(
            json.dumps(
                {
                    "event": "staff_template_text_not_inspected",
                    "conversation_id": conversation_id,
                    "staff_reply_id": staff_reply_id,
                }
            )
        )
        return False
    sender = _sender_for_staff_reply_or_log_failure(
        conversation_id=conversation_id, staff_reply_id=staff_reply_id
    )
    if sender is None:
        return False
    try:
        whatsapp_message_id = await sender.send_template(
            to_phone=customer_phone.removeprefix("+"),
            template_name=template.template_name,
            language_code=template.language_code,
            body_parameters=template.body_parameters,
        )
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "whatsapp_template_send_failed",
                    "conversation_id": conversation_id,
                    "staff_reply_id": staff_reply_id,
                    "template_name": template.template_name,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )
        return False
    try:
        _insert_outbound_message(
            conn,
            conversation_id=conversation_id,
            customer_phone=customer_phone,
            whatsapp_message_id=whatsapp_message_id,
            body=inspection.text,
            staff_reply_id=staff_reply_id,
        )
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "outbound_message_not_recorded",
                    "conversation_id": conversation_id,
                    "whatsapp_message_id": whatsapp_message_id,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )
    return True


async def _escalate_and_notify(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    customer_phone: str,
    reason: str,
    exc: Exception | None,
    existing_escalation_id: int | None = None,
    notice: FixedText = FALLBACK,
    extra_notes: dict[str, str] | None = None,
) -> str:
    """The one funnel every failed turn goes through (CLAUDE.md rule 12,
    "never leave a customer in silence", and §9's "beyond the cap,
    escalate to a human"): opens an escalation for a human and sends the
    customer the fallback message, in their language -- or, for a voice
    note or image, notice=fixed_texts.PLEASE_TYPE, with the message type in
    extra_notes.

    The two are attempted independently -- a failure to open the
    escalation no longer stops the fallback from being sent, and a failed
    send is reported as such (escalated_undelivered) instead of as
    "escalated". existing_escalation_id is for a caller whose escalation
    already exists (the output guard opens its own for a blocked reply,
    and a cap may already be escalated today for this number): no second
    one is opened, only the fallback is sent.

    If either half failed because the turn's own connection died partway
    through (psycopg reports a broken connection as closed), the failed
    half -- only that half, so the customer is never sent a second
    fallback -- is retried once on a fresh connection: one lost
    connection, with the database itself still up, must not become a
    silent turn.

    The one exception to sending the fallback: a staff member took the
    conversation over while the turn ran. The escalation still opens, for
    them to see, but the customer gets nothing from the bot (the approved
    takeover silence, ARCHITECTURE.md §7).

    Returns one of the four _STATUS_ESCALATED* / _STATUS_NOTIFIED_* /
    _STATUS_FAILED_UNRECORDED labels, or _STATUS_SUPPRESSED_TAKEN_OVER.
    Whatever usage the turn incurred is the caller's to have recorded
    before this runs; this function never records usage. Never raises.
    """
    escalation_id = existing_escalation_id
    if escalation_id is None:
        escalation_id = _open_escalation_or_log_failure(
            conn,
            conversation_id=conversation_id,
            reason=reason,
            exc=exc,
            extra_notes=extra_notes,
        )
    if _send_suppressed_by_takeover(
        conn, conversation_id=conversation_id, withheld="fallback"
    ):
        return _STATUS_SUPPRESSED_TAKEN_OVER
    delivered = await _send_fallback_or_log_failure(
        conn,
        conversation_id=conversation_id,
        customer_phone=customer_phone,
        reason=reason,
        escalation_id=escalation_id,
        notice=notice,
    )
    if (escalation_id is None or not delivered) and conn.closed:
        escalation_id, delivered = await _retry_funnel_on_a_fresh_connection(
            conversation_id=conversation_id,
            customer_phone=customer_phone,
            reason=reason,
            exc=exc,
            escalation_id=escalation_id,
            delivered=delivered,
            notice=notice,
            extra_notes=extra_notes,
        )
    return _funnel_status(escalated=escalation_id is not None, delivered=delivered)


async def _retry_funnel_on_a_fresh_connection(
    *,
    conversation_id: int,
    customer_phone: str,
    reason: str,
    exc: Exception | None,
    escalation_id: int | None,
    delivered: bool,
    notice: FixedText,
    extra_notes: dict[str, str] | None,
) -> tuple[int | None, bool]:
    """Re-attempts whichever of the escalation and the fallback send has
    not succeeded yet, on a new connection, and returns the updated pair.
    A database that cannot be reached at all stays the documented
    residual (ARCHITECTURE.md §7, "لا صمت"): logged, not raised."""
    try:
        with get_db_connection() as fresh_conn:
            if escalation_id is None:
                escalation_id = _open_escalation_or_log_failure(
                    fresh_conn,
                    conversation_id=conversation_id,
                    reason=reason,
                    exc=exc,
                    extra_notes=extra_notes,
                )
            if not delivered:
                delivered = await _send_fallback_or_log_failure(
                    fresh_conn,
                    conversation_id=conversation_id,
                    customer_phone=customer_phone,
                    reason=reason,
                    escalation_id=escalation_id,
                    notice=notice,
                )
    except Exception as connect_exc:
        logger.error(
            json.dumps(
                {
                    "event": "funnel_reconnect_failed",
                    "conversation_id": conversation_id,
                    "reason": reason,
                    "exception_type": type(connect_exc).__name__,
                    "exception_message": str(connect_exc),
                }
            ),
            exc_info=connect_exc,
        )
    return escalation_id, delivered


def _todays_cap_escalation_or_none(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    customer_phone: str,
    reason: str,
    now: datetime,
) -> int | None:
    """The cap escalation already opened today for customer_phone
    (caps.find_todays_cap_escalation), to reuse instead of opening another,
    or None. A failed lookup is logged and returns None, so a new
    escalation is opened: a duplicate is the safe side of that trade, a
    missed escalation is not. Never raises."""
    try:
        escalation_id = find_todays_cap_escalation(
            conn,
            customer_phone=customer_phone,
            reasons=_COLLAPSED_CAP_REASONS,
            now=now,
        )
    except Exception as lookup_exc:
        logger.error(
            json.dumps(
                {
                    "event": "cap_escalation_lookup_failed",
                    "conversation_id": conversation_id,
                    "reason": reason,
                    "exception_type": type(lookup_exc).__name__,
                }
            )
        )
        return None
    if escalation_id is not None:
        logger.info(
            json.dumps(
                {
                    "event": "cap_escalation_reused",
                    "conversation_id": conversation_id,
                    "reason": reason,
                    "escalation_id": escalation_id,
                }
            )
        )
    return escalation_id


async def _handle_generate_reply_failure(
    conn: psycopg.Connection[Any],
    exc: Exception,
    *,
    conversation_id: int,
    customer_phone: str,
    now: datetime,
) -> str:
    """The single path for everything generate_reply can raise.

    Records whatever usage the exception is carrying — present only when
    one or more real model calls already happened this turn before it
    was raised, per errors.read_usage_so_far and conversation.py's own
    docstring — and increments turn_count alongside it (same condition,
    same reasoning as caps.increment_turn_count's own docstring: a real
    model call happened, so this turn counts, regardless of how it
    ends). Then EVERY exception, of any type, goes through
    _escalate_and_notify with _escalation_reason's reason -- CLAUDE.md
    rule 12: no exception type may end a turn in silence. For the four
    _COLLAPSED_CAP_STOPS, a cap escalation already opened today for this
    number is reused, so the customer gets the fallback without a second
    escalation (ARCHITECTURE.md §7).

    Anything other than an anticipated stop is also logged at ERROR:
    usage_unavailable (just the event) for UsageUnavailableError, and
    turn_failed for the rest, with the type and a frames-only traceback,
    never the message (see _safe_exception_log_fields). Adding a new
    exception type to generate_reply's loop in the future needs no change
    here.

    Must be called outside any `except` block for exc: an exception
    raised while this runs must not carry exc as its __context__, or a
    rendered traceback would print exc's message after all (see
    _generate_reply_or_exception).
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
        _increment_turn_count_or_log_failure(conn, conversation_id=conversation_id)

    if isinstance(exc, UsageUnavailableError):
        logger.error(
            json.dumps(
                {"event": "usage_unavailable", "conversation_id": conversation_id}
            )
        )
    elif not isinstance(exc, _ANTICIPATED_STOPS):
        # Deliberately loud: CLAUDE.md forbids folding a real bug or a
        # database outage quietly into a generic bucket -- but loud
        # without the exception's text, which for a services.pricing error
        # holds cost and floor values.
        logger.error(
            json.dumps(
                {
                    "event": "turn_failed",
                    "conversation_id": conversation_id,
                    **_safe_exception_log_fields(exc),
                }
            )
        )

    reason = _escalation_reason(exc)
    existing_escalation_id = None
    if isinstance(exc, _COLLAPSED_CAP_STOPS):
        existing_escalation_id = _todays_cap_escalation_or_none(
            conn,
            conversation_id=conversation_id,
            customer_phone=customer_phone,
            reason=reason,
            now=now,
        )
    return await _escalate_and_notify(
        conn,
        conversation_id=conversation_id,
        customer_phone=customer_phone,
        reason=reason,
        exc=exc,
        existing_escalation_id=existing_escalation_id,
    )


async def _process_turn(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    customer_name: str | None,
    customer_phone: str,
    message: CustomerMessage,
    llm_settings: LlmSettings,
    now: datetime,
) -> str:
    """Steps 4-7 of the module docstring: the model call through the
    output-guard-checked send, plus the staff follow-up for dates not open
    for booking (services/agent/staff_follow_up.py) when the turn's tools
    reported any. A booking yes that code answers (services/agent/
    booking_yes.py) skips the model entirely: no model call, no usage, no
    turn counted. A reply offering exactly one priced stay goes out with
    the booking buttons (booking_buttons.buttons_for_reply). Returns a
    status label that _generate_and_deliver_reply logs; no caller reads it
    as an HTTP response anymore. Every path ends in either the reply
    delivered ("processed") or _escalate_and_notify (CLAUDE.md rule 12).
    """
    decision = _booking_decision_or_none(
        conn, message, conversation_id=conversation_id, llm_settings=llm_settings
    )
    if decision is not None:
        return await _answer_booking_decision(
            conn,
            decision,
            conversation_id=conversation_id,
            customer_phone=customer_phone,
            llm_settings=llm_settings,
        )
    transport = get_model_transport(llm_settings)
    outcome = await _generate_reply_or_exception(
        conn,
        conversation_id=conversation_id,
        customer_name=customer_name,
        transport=transport,
        llm_settings=llm_settings,
        now=now,
    )
    if isinstance(outcome, Exception):
        return await _handle_generate_reply_failure(
            conn,
            outcome,
            conversation_id=conversation_id,
            customer_phone=customer_phone,
            now=now,
        )

    # A failed usage write is its own ERROR event (record_token_usage_
    # failed); it no longer overwrites the delivery status, which would
    # hide whether the customer got an answer.
    _record_usage_or_log_failure(
        conn,
        conversation_id=conversation_id,
        customer_phone=customer_phone,
        usage=outcome.usage,
        now=now,
    )
    _increment_turn_count_or_log_failure(conn, conversation_id=conversation_id)
    open_follow_up_for_dates_not_open(
        conn, conversation_id=conversation_id, tool_calls=outcome.tool_calls
    )

    passed_on_quote_id = _passed_on_quote_id(outcome)
    if passed_on_quote_id is not None:
        return await _confirm_model_booking_request(
            conn,
            quote_id=passed_on_quote_id,
            conversation_id=conversation_id,
            customer_phone=customer_phone,
            llm_settings=llm_settings,
        )
    return await _deliver_reply(
        conn,
        conversation_id=conversation_id,
        customer_phone=customer_phone,
        reply_text=outcome.text,
        quote_validity=llm_settings.quote_validity,
        offer=buttons_for_reply(
            to_whatsapp_formatting(outcome.text), outcome.quote_ids
        ),
    )


def _passed_on_quote_id(outcome: AgentReply) -> int | None:
    """The quote a request_booking_follow_up call passed on this turn (the
    last, if there were several), or None when none succeeded."""
    passed_on = [
        int(call.result["quote_id"])
        for call in outcome.tool_calls
        if call.name == REQUEST_BOOKING_FOLLOW_UP_TOOL
        and call.result.get("requested") is True
    ]
    return passed_on[-1] if passed_on else None


async def _confirm_model_booking_request(
    conn: psycopg.Connection[Any],
    *,
    quote_id: int,
    conversation_id: int,
    customer_phone: str,
    llm_settings: LlmSettings,
) -> str:
    """Sends the fixed confirmation in place of the model's reply after the
    model passed a booking on: the confirmation is never model-written
    (owner decision 2026-10-01).

    Raises:
        PassedOnQuoteNotFoundError, psycopg.Error: see
            booking_yes.confirmation_for_passed_on_quote -- the caller's
            last-resort net escalates and sends the fallback.
    """
    logger.info(
        json.dumps(
            {
                "event": "booking_request_confirmed_in_code",
                "conversation_id": conversation_id,
                "quote_id": quote_id,
            }
        )
    )
    return await _deliver_reply(
        conn,
        conversation_id=conversation_id,
        customer_phone=customer_phone,
        reply_text=confirmation_for_passed_on_quote(
            conn, conversation_id=conversation_id, quote_id=quote_id
        ),
        quote_validity=llm_settings.quote_validity,
        booking_passed_on=True,
    )


def _booking_decision_or_none(
    conn: psycopg.Connection[Any],
    message: CustomerMessage,
    *,
    conversation_id: int,
    llm_settings: LlmSettings,
) -> BookingDecision | None:
    """decide_booking_yes, with a failed read logged at ERROR and answered
    by the model instead: the model path still has the booking tool, and
    its own failures end in the funnel. Never raises."""
    try:
        return decide_booking_yes(
            conn,
            message,
            conversation_id=conversation_id,
            quote_validity=llm_settings.quote_validity,
        )
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "booking_yes_check_failed",
                    "conversation_id": conversation_id,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )
        return None


async def _answer_booking_decision(
    conn: psycopg.Connection[Any],
    decision: BookingDecision,
    *,
    conversation_id: int,
    customer_phone: str,
    llm_settings: LlmSettings,
) -> str:
    """Acts on a booking yes code answers, without a model call: a
    mismatched button gets the funnel (fallback and escalation); a tap on
    a replaced offer gets fixed_texts.NEWER_PRICE with buttons for the
    newer quote; otherwise the quote is passed on (one escalation per
    quote, booking_follow_up.pass_quote_on) and confirmed. Every text goes
    through _deliver_reply and so the output guard.

    Raises:
        psycopg.Error: pass_quote_on's write failed -- the caller's
            last-resort net escalates and sends the fallback.
    """
    if isinstance(decision, ButtonMismatch):
        logger.warning(
            json.dumps(
                {
                    "event": "booking_button_mismatch",
                    "conversation_id": conversation_id,
                    "problem": decision.problem,
                }
            )
        )
        return await _escalate_and_notify(
            conn,
            conversation_id=conversation_id,
            customer_phone=customer_phone,
            reason=_REASON_BOOKING_BUTTON_MISMATCH,
            exc=None,
            extra_notes={"problem": decision.problem},
        )
    if isinstance(decision, OfferNewerPrice):
        return await _offer_newer_price(
            conn,
            decision,
            conversation_id=conversation_id,
            customer_phone=customer_phone,
            llm_settings=llm_settings,
        )
    return await _pass_on_and_confirm(
        conn,
        decision,
        conversation_id=conversation_id,
        customer_phone=customer_phone,
        llm_settings=llm_settings,
    )


async def _offer_newer_price(
    conn: psycopg.Connection[Any],
    decision: OfferNewerPrice,
    *,
    conversation_id: int,
    customer_phone: str,
    llm_settings: LlmSettings,
) -> str:
    logger.info(
        json.dumps(
            {
                "event": "booking_button_newer_price_offered",
                "conversation_id": conversation_id,
                "tapped_quote_id": decision.tapped_quote_id,
                "quote_id": decision.quote_id,
            }
        )
    )
    return await _deliver_reply(
        conn,
        conversation_id=conversation_id,
        customer_phone=customer_phone,
        reply_text=NEWER_PRICE.render(decision.language),
        quote_validity=llm_settings.quote_validity,
        offer=booking_offer_buttons(decision.quote_id, decision.language),
    )


async def _pass_on_and_confirm(
    conn: psycopg.Connection[Any],
    decision: PassOn,
    *,
    conversation_id: int,
    customer_phone: str,
    llm_settings: LlmSettings,
) -> str:
    result = pass_quote_on(conn, conversation_id=conversation_id, quote=decision.quote)
    logger.info(
        json.dumps(
            {
                "event": "booking_yes_handled_in_code",
                "conversation_id": conversation_id,
                "quote_id": decision.quote.quote_id,
                "already_requested": result["already_requested"],
                "source": decision.source,
            }
        )
    )
    return await _deliver_reply(
        conn,
        conversation_id=conversation_id,
        customer_phone=customer_phone,
        reply_text=render_booking_passed_on(decision.quote, decision.language),
        quote_validity=llm_settings.quote_validity,
        booking_passed_on=True,
    )


async def _generate_reply_or_exception(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    customer_name: str | None,
    transport: ModelTransport,
    llm_settings: LlmSettings,
    now: datetime,
) -> AgentReply | Exception:
    """generate_reply, with any exception returned rather than raised.

    Returned, not handled here, so the caller can pass it to
    _handle_generate_reply_failure OUTSIDE this `except` block: anything
    raised while an exception is being handled gets it as __context__,
    and a rendered traceback of that later exception would print this
    one's message -- which for a services.pricing error holds cost and
    floor values (CLAUDE.md §8).
    """
    try:
        return await generate_reply(
            conn,
            conversation_id=conversation_id,
            customer_name=customer_name,
            transport=transport,
            settings=llm_settings,
            now=now,
        )
    except Exception as exc:
        return exc


def _undeliverable_reply_reason(text: str) -> str | None:
    """Why a model reply cannot be sent as it is, or None if it can: blank
    (the model returned no text, or only whitespace -- nothing to send),
    or longer than the Cloud API's text.body limit."""
    if not text.strip():
        return _REASON_EMPTY_REPLY
    if len(text) > WHATSAPP_TEXT_BODY_MAX_CHARS:
        return _REASON_REPLY_TOO_LONG
    return None


def _check_reply_and_prepare_sender(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    text: str,
    quote_validity: timedelta,
    booking_passed_on: bool,
) -> WhatsAppSender | GuardVerdict | Exception:
    """Runs the output guard on the reply, then builds the WhatsApp sender
    for an allowed one. Returns the sender (allowed), the blocking
    GuardVerdict (blocked -- the guard has already opened its escalation),
    or the exception if the guard or the send setup raised. Returned
    rather than raised for the same reason as
    _generate_reply_or_exception: the caller hands it to the funnel
    outside this `except` block. booking_passed_on: see
    enforce_outbound_text."""
    try:
        verdict = enforce_outbound_text(
            conn,
            conversation_id=conversation_id,
            text=text,
            quote_validity=quote_validity,
            booking_passed_on=booking_passed_on,
        )
        if not verdict.allowed:
            return verdict
        return get_whatsapp_sender(get_whatsapp_send_settings())
    except Exception as exc:
        return exc


@dataclass(frozen=True)
class _DeliveryFailure:
    """Why a model reply was not delivered, in the terms
    _escalate_and_notify takes."""

    reason: str
    exc: Exception | None = None
    existing_escalation_id: int | None = None


async def _send_reply(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    customer_phone: str,
    reply_text: str,
    quote_validity: timedelta,
    offer: BookingOfferButtons | None,
    booking_passed_on: bool,
) -> _DeliveryFailure | None:
    """Sends a reply, with offer's buttons when given; returns None when it
    was delivered, otherwise why not: blank or over-length (not sendable at
    all), a guard or send-setup error, a guard block, or a failed send.
    Never raises.

    The reply is converted to WhatsApp formatting before the guard checks
    it: the guard must validate exactly what will be sent, and a
    Markdown-bold price ("**343.85 SAR**") must not be checked in a form
    the customer will never see. For the same reason the button titles are
    checked with the body (booking_buttons.text_with_button_titles).
    booking_passed_on is True only for the code-rendered confirmation of a
    booking passed on this turn: any other text claiming one is blocked
    (output_guard.booking_claims).
    """
    formatted_text = to_whatsapp_formatting(reply_text)
    undeliverable_reason = _undeliverable_reply_reason(formatted_text)
    if undeliverable_reason is not None:
        logger.error(
            json.dumps(
                {
                    "event": "reply_undeliverable",
                    "conversation_id": conversation_id,
                    "reason": undeliverable_reason,
                    "length": len(formatted_text),
                }
            )
        )
        return _DeliveryFailure(reason=undeliverable_reason)

    prepared = _check_reply_and_prepare_sender(
        conn,
        conversation_id=conversation_id,
        text=text_with_button_titles(formatted_text, offer),
        quote_validity=quote_validity,
        booking_passed_on=booking_passed_on,
    )
    if isinstance(prepared, Exception):
        logger.error(
            json.dumps(
                {
                    "event": "reply_delivery_failed",
                    "conversation_id": conversation_id,
                    "exception_type": type(prepared).__name__,
                    "exception_message": str(prepared),
                }
            ),
            exc_info=prepared,
        )
        return _DeliveryFailure(reason=_REASON_DELIVERY_FAILED, exc=prepared)
    if isinstance(prepared, GuardVerdict):
        return _DeliveryFailure(
            reason=_REASON_OUTPUT_GUARD_BLOCKED,
            existing_escalation_id=prepared.escalation_id,
        )

    whatsapp_message_id = await _send_or_log_failure(
        prepared,
        conn=conn,
        conversation_id=conversation_id,
        customer_phone=customer_phone,
        text=formatted_text,
        offer=offer,
    )
    if whatsapp_message_id is None:
        return _DeliveryFailure(reason=_REASON_DELIVERY_FAILED)
    return None


async def _deliver_reply(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    customer_phone: str,
    reply_text: str,
    quote_validity: timedelta,
    offer: BookingOfferButtons | None = None,
    booking_passed_on: bool = False,
) -> str:
    """Delivers a reply ("processed"), with offer's buttons when given, or
    hands the turn to _escalate_and_notify for whatever stopped it
    (_send_reply). Sends nothing if a staff member took the conversation
    over while the turn ran (suppressed_taken_over). booking_passed_on: see
    _send_reply. Never raises."""
    if _send_suppressed_by_takeover(
        conn, conversation_id=conversation_id, withheld="reply"
    ):
        return _STATUS_SUPPRESSED_TAKEN_OVER
    failure = await _send_reply(
        conn,
        conversation_id=conversation_id,
        customer_phone=customer_phone,
        reply_text=reply_text,
        quote_validity=quote_validity,
        offer=offer,
        booking_passed_on=booking_passed_on,
    )
    if failure is None:
        return _STATUS_PROCESSED
    return await _escalate_and_notify(
        conn,
        conversation_id=conversation_id,
        customer_phone=customer_phone,
        reason=failure.reason,
        exc=failure.exc,
        existing_escalation_id=failure.existing_escalation_id,
    )


async def _escalate_unexpected_background_failure(
    exc: Exception, *, conversation_id: int, customer_phone: str
) -> str:
    """Last-resort safety net for _generate_and_deliver_reply: something
    escaped every handler already in this module -- a bug, the model
    transport failing to build (get_model_transport), or the turn's own
    DB connection failing to open. Before the fast-ack/background split,
    that would have surfaced as an HTTP 500 Meta could see and retry --
    moot even then, since idempotent dedup means a retry can never reach
    generate_reply again (see the module docstring) -- but now there is no
    response at all left to carry it, so this is the one place that must
    still open an escalation and attempt the fallback send. Uses a fresh
    connection of its own, since the turn's may be why this failed.

    Reuses the funnel rather than duplicating it (CLAUDE.md §2's "one way
    to do each thing"), with reason _REASON_INTERNAL_ERROR. Returns the
    funnel status. Never raises: there is nothing left to hand a failure
    to.
    """
    return await escalate_and_notify_on_own_connection(
        conversation_id=conversation_id,
        customer_phone=customer_phone,
        reason=_REASON_INTERNAL_ERROR,
        exc=exc,
    )


async def escalate_and_notify_on_own_connection(
    *,
    conversation_id: int,
    customer_phone: str,
    reason: str,
    exc: Exception | None,
    notice: FixedText = FALLBACK,
    extra_notes: dict[str, str] | None = None,
) -> str:
    """_escalate_and_notify on a connection of its own, for a background
    job that has none: the last-resort safety net, the notices the fast
    path schedules for a message the model never sees (a voice note, an
    image, other media, the first message past the daily cap), and the
    startup sweep (services/agent/startup_sweep.py). Returns the funnel
    status, or failed_unrecorded if not even a connection can be opened.
    Never raises."""
    try:
        with get_db_connection() as conn:
            return await _escalate_and_notify(
                conn,
                conversation_id=conversation_id,
                customer_phone=customer_phone,
                reason=reason,
                exc=exc,
                notice=notice,
                extra_notes=extra_notes,
            )
    except Exception as unexpected_exc:
        logger.error(
            json.dumps(
                {
                    "event": "background_reply_escalation_failed",
                    "conversation_id": conversation_id,
                    "reason": reason,
                    "exception_type": type(unexpected_exc).__name__,
                    "exception_message": str(unexpected_exc),
                }
            ),
            exc_info=unexpected_exc,
        )
        return _STATUS_FAILED_UNRECORDED


async def _process_turn_or_exception(
    *,
    conversation_id: int,
    customer_name: str | None,
    customer_phone: str,
    message: CustomerMessage,
    llm_settings: LlmSettings,
    now: datetime,
) -> str | Exception:
    """_process_turn on a connection of its own, with anything it raises
    returned rather than raised -- so the caller handles it outside this
    `except` block, for the same __context__ reason as
    _generate_reply_or_exception."""
    try:
        with get_db_connection() as conn:
            return await _process_turn(
                conn,
                conversation_id=conversation_id,
                customer_name=customer_name,
                customer_phone=customer_phone,
                message=message,
                llm_settings=llm_settings,
                now=now,
            )
    except Exception as exc:
        return exc


async def _generate_and_deliver_reply(
    *,
    conversation_id: int,
    customer_name: str | None,
    customer_phone: str,
    message: CustomerMessage,
    llm_settings: LlmSettings,
    now: datetime,
) -> None:
    """The slow half of receive_message: steps 4-7 of the module
    docstring, run via FastAPI BackgroundTasks after the fast ack.
    Opens its own DB connection -- the one receive_message used for the
    fast path is already closed by the time Starlette runs this job
    (after the response is sent). Logs the turn's status as
    reply_turn_finished.

    Never raises: unlike the request this used to run inside, there is
    no HTTP response left downstream to carry a failure back to Meta
    once this is scheduled -- nothing reads an exception from here.
    Every known failure mode is already handled inside _process_turn
    (_handle_generate_reply_failure and _deliver_reply, both ending in
    _escalate_and_notify); anything that still escapes it -- e.g.
    get_db_connection() or get_model_transport() failing -- gets an
    ERROR log and the same funnel through
    _escalate_unexpected_background_failure, instead of vanishing into
    Starlette's own background-task exception log with no trace and no
    customer notification.
    """
    outcome = await _process_turn_or_exception(
        conversation_id=conversation_id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        message=message,
        llm_settings=llm_settings,
        now=now,
    )
    if isinstance(outcome, Exception):
        logger.error(
            json.dumps(
                {
                    "event": "background_reply_failed",
                    "conversation_id": conversation_id,
                    "exception_type": type(outcome).__name__,
                    "exception_message": str(outcome),
                }
            ),
            exc_info=outcome,
        )
        status = await _escalate_unexpected_background_failure(
            outcome, conversation_id=conversation_id, customer_phone=customer_phone
        )
    else:
        status = outcome
    logger.info(
        json.dumps(
            {
                "event": "reply_turn_finished",
                "conversation_id": conversation_id,
                "status": status,
            }
        )
    )


async def _send_notice_without_a_model_turn(
    *,
    conversation_id: int,
    customer_phone: str,
    reason: str,
    notice: FixedText,
    extra_notes: dict[str, str] | None,
) -> None:
    """Background job for a stored message the model never sees -- a
    voice note, an image, other media, or the first message past the
    daily cap: the funnel on a connection of its own (CLAUDE.md rule 12),
    then the same reply_turn_finished line every turn logs. Never
    raises."""
    status = await escalate_and_notify_on_own_connection(
        conversation_id=conversation_id,
        customer_phone=customer_phone,
        reason=reason,
        exc=None,
        notice=notice,
        extra_notes=extra_notes,
    )
    logger.info(
        json.dumps(
            {
                "event": "reply_turn_finished",
                "conversation_id": conversation_id,
                "status": status,
            }
        )
    )


def _touch_last_message_at_or_log_failure(
    conn: psycopg.Connection[Any], *, conversation_id: int
) -> None:
    """touch_last_message_at, logged at ERROR instead of raised: it runs
    after the inbound message is stored, where an exception would turn
    into a 500 whose retry is then dropped as a duplicate -- a message
    lost for good. The session clock is off by one message at worst."""
    try:
        touch_last_message_at(conn, conversation_id=conversation_id)
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "touch_last_message_at_failed",
                    "conversation_id": conversation_id,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )


def _rate_cap_blocks(
    conn: psycopg.Connection[Any],
    inbound: InboundMessage,
    *,
    conversation_id: int,
    llm_settings: LlmSettings,
    now: datetime,
) -> MessageRateCapExceededError | None:
    """The rate-cap verdict for a message already stored: the cap error if
    it is blocked, else None. A failed check (a database error, not the
    cap) is logged at ERROR and lets the message through -- for the same
    lost-for-good reason as _touch_last_message_at_or_log_failure, and
    because one message over the cap is a smaller harm than silence.
    Never raises."""
    try:
        check_message_rate_cap(
            conn, customer_phone=inbound.customer_phone, now=now, settings=llm_settings
        )
    except MessageRateCapExceededError as cap_exc:
        logger.info(
            json.dumps(
                {
                    "event": "message_rate_cap_blocked",
                    "conversation_id": conversation_id,
                    "first_of_day": cap_exc.first_of_day,
                }
            )
        )
        return cap_exc
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "message_rate_cap_check_failed",
                    "conversation_id": conversation_id,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )
    return None


def _schedule_after_storing(
    inbound: InboundMessage,
    *,
    conversation_id: int,
    llm_settings: LlmSettings,
    now: datetime,
    background_tasks: BackgroundTasks,
) -> str:
    """Schedules the background job for a stored, uncapped message: a
    turn for text or a tapped button, a notice for any other handled type
    (decision A: fixed_texts.PLEASE_TYPE for a voice note or an image, the
    fallback for other media, an escalation either way). Returns its
    status."""
    if inbound.message_type in _TURN_MESSAGE_TYPES:
        background_tasks.add_task(
            _generate_and_deliver_reply,
            conversation_id=conversation_id,
            customer_name=inbound.customer_name,
            customer_phone=inbound.customer_phone,
            message=CustomerMessage(
                whatsapp_message_id=inbound.whatsapp_message_id,
                text=inbound.body,
                button=inbound.button,
            ),
            llm_settings=llm_settings,
            now=now,
        )
        return _STATUS_ACCEPTED
    notice = (
        PLEASE_TYPE if inbound.message_type in _PLEASE_TYPE_MESSAGE_TYPES else FALLBACK
    )
    background_tasks.add_task(
        _send_notice_without_a_model_turn,
        conversation_id=conversation_id,
        customer_phone=inbound.customer_phone,
        reason=_REASON_UNSUPPORTED_MESSAGE_TYPE,
        notice=notice,
        extra_notes={"message_type": inbound.message_type},
    )
    return _STATUS_UNSUPPORTED_TYPE


def _accept_inbound_message(
    conn: psycopg.Connection[Any],
    inbound: InboundMessage,
    *,
    llm_settings: LlmSettings,
    now: datetime,
    background_tasks: BackgroundTasks,
) -> str:
    """Stores one inbound message and schedules what answers it. Returns
    its status. A message to a conversation a staff member has taken over
    is stored and nothing is scheduled -- not even the "please type" notice
    or the rate-cap fallback, and the rate cap is not checked (owner
    decision D6, 2026-10-02).

    Everything before the insert may raise -- the message is not stored
    yet, so a retry can still process it. Everything after may not: the
    stored row makes any redelivery a duplicate, so a failure there must
    never become a 500 (the post-insert steps log and carry on).

    Raises:
        Any exception from the conversation upsert, the session check or
        the insert itself (see _accept_inbound_message_or_log_failure).
    """
    if inbound.message_type not in _HANDLED_MESSAGE_TYPES:
        logger.info(
            json.dumps(
                {
                    "event": "inbound_message_ignored",
                    "message_type": inbound.message_type,
                }
            )
        )
        return _STATUS_IGNORED
    conversation_id = _find_or_create_conversation(
        conn, customer_phone=inbound.customer_phone
    )
    start_new_session_if_idle(conn, conversation_id=conversation_id, now=now)
    message_id = _insert_inbound_message(
        conn,
        conversation_id=conversation_id,
        customer_phone=inbound.customer_phone,
        whatsapp_message_id=inbound.whatsapp_message_id,
        body=inbound.body,
    )
    if message_id is None:
        return _STATUS_DUPLICATE

    _touch_last_message_at_or_log_failure(conn, conversation_id=conversation_id)
    if is_taken_over(conn, conversation_id=conversation_id):
        logger.info(
            json.dumps(
                {
                    "event": "inbound_while_taken_over",
                    "conversation_id": conversation_id,
                    "message_type": inbound.message_type,
                }
            )
        )
        return _STATUS_TAKEN_OVER
    cap_exc = _rate_cap_blocks(
        conn,
        inbound,
        conversation_id=conversation_id,
        llm_settings=llm_settings,
        now=now,
    )
    if cap_exc is not None:
        if cap_exc.first_of_day:
            background_tasks.add_task(
                _send_notice_without_a_model_turn,
                conversation_id=conversation_id,
                customer_phone=inbound.customer_phone,
                reason=_REASON_MESSAGE_RATE_CAP_EXCEEDED,
                notice=FALLBACK,
                extra_notes=None,
            )
        return _STATUS_RATE_LIMITED
    return _schedule_after_storing(
        inbound,
        conversation_id=conversation_id,
        llm_settings=llm_settings,
        now=now,
        background_tasks=background_tasks,
    )


def _accept_inbound_message_or_log_failure(
    conn: psycopg.Connection[Any],
    inbound: InboundMessage,
    *,
    llm_settings: LlmSettings,
    now: datetime,
    background_tasks: BackgroundTasks,
) -> str:
    """_accept_inbound_message, with a failure before the message was
    stored logged at ERROR and reported as not_stored, so one bad message
    does not stop the rest of a batch; receive_message then answers 500
    for the retry. Never raises."""
    try:
        return _accept_inbound_message(
            conn,
            inbound,
            llm_settings=llm_settings,
            now=now,
            background_tasks=background_tasks,
        )
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "inbound_message_not_stored",
                    "message_type": inbound.message_type,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )
        return _STATUS_NOT_STORED


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
async def receive_message(
    request: Request, background_tasks: BackgroundTasks
) -> JSONResponse:
    """Processes one WhatsApp Cloud API webhook delivery -- every message
    in it (_accept_inbound_message) -- see this module's own docstring for
    the fast-ack/background split and for why the fast path stops where
    it does.

    The response body is {"status": ...} for a single message (or
    "ignored" when there is none), and {"status": "batch", "results":
    [...]} for several. It is HTTP 500 when any message could not be
    stored, so Meta redelivers the batch: the messages already stored come
    back as duplicates and were already answered -- FastAPI still runs the
    jobs scheduled here on a returned 500 -- and the failed one gets
    another attempt.

    Raises:
        HTTPException(401): the signature is missing or does not match
            X-Hub-Signature-256 -- checked before any database access, and
            logged as a warning with the reason only.
        HTTPException(400): the signed body is not valid JSON.
    """
    webhook_settings = get_webhook_settings()
    body = await request.body()
    problem = _signature_problem(
        body=body,
        signature_header=request.headers.get(_SIGNATURE_HEADER),
        app_secret=webhook_settings.app_secret,
    )
    if problem is not None:
        # The reason and the body size only: never the header's value or
        # the app secret. A wrong WHATSAPP_APP_SECRET would otherwise make
        # every real message vanish with no application log at all.
        logger.warning(
            json.dumps(
                {
                    "event": "webhook_signature_rejected",
                    "problem": problem,
                    "body_bytes": len(body),
                }
            )
        )
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body") from None

    messages = _parse_inbound_messages(_as_dict(payload))
    if not messages:
        return JSONResponse({"status": _STATUS_IGNORED})

    llm_settings = get_llm_settings()
    now = datetime.now(UTC)
    with get_db_connection() as conn:
        statuses = [
            _accept_inbound_message_or_log_failure(
                conn,
                inbound,
                llm_settings=llm_settings,
                now=now,
                background_tasks=background_tasks,
            )
            for inbound in messages
        ]

    status_code = 500 if _STATUS_NOT_STORED in statuses else 200
    if len(statuses) == 1:
        return JSONResponse({"status": statuses[0]}, status_code=status_code)
    return JSONResponse(
        {"status": _STATUS_BATCH, "results": statuses}, status_code=status_code
    )
