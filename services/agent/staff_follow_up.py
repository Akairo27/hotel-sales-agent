"""The staff follow-up for dates not open for booking yet — owner decision,
2026-09-29 (ARCHITECTURE.md §7).

When check_availability or get_quote reports nights_without_allotment (no
inventory row: those dates have not been opened for sale), the bot must
not call them fully booked; it tells the customer the dates are not open
for booking yet and that a colleague will follow up (prompt.py's
unavailable_dates rule). This module opens that follow-up: one escalation
per turn, reason dates_not_open_for_booking, listing every such night per
hotel and room type the turn's tool calls reported, however many calls
reported them. webhook._process_turn calls it once generate_reply has
returned; a failed turn goes through the no-silence funnel instead, which
escalates on its own.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

import psycopg

from services.agent.llm.conversation import ToolCallRecord
from services.agent.output_guard.enforcement import open_escalation

logger = logging.getLogger(__name__)

REASON_DATES_NOT_OPEN_FOR_BOOKING = "dates_not_open_for_booking"


def nights_not_open_for_booking(
    tool_calls: Sequence[ToolCallRecord],
) -> list[dict[str, Any]]:
    """Every night the turn's tool results listed in nights_without_allotment,
    grouped by (hotel_id, room_type_id), each group's nights sorted and
    de-duplicated. Empty when no result listed any. Reads only results
    dispatch.py built (never model text): a tool error result or a priced
    quote has no such list and is skipped."""
    nights_by_stay: dict[tuple[int, int], set[str]] = {}
    for call in tool_calls:
        nights = call.result.get("nights_without_allotment")
        if not nights:
            continue
        stay = (int(call.result["hotel_id"]), int(call.result["room_type_id"]))
        nights_by_stay.setdefault(stay, set()).update(nights)
    return [
        {"hotel_id": hotel_id, "room_type_id": room_type_id, "nights": sorted(nights)}
        for (hotel_id, room_type_id), nights in sorted(nights_by_stay.items())
    ]


def open_follow_up_for_dates_not_open(
    conn: psycopg.Connection[Any],
    *,
    conversation_id: int,
    tool_calls: Sequence[ToolCallRecord],
) -> int | None:
    """Opens the turn's one dates_not_open_for_booking escalation, with notes
    {"stays": nights_not_open_for_booking(tool_calls)}, and logs
    dates_not_open_escalated. Returns its id, or None when there is
    nothing to follow up or the insert failed -- logged at ERROR as
    dates_not_open_escalation_failed. Never raises: the customer's reply is
    delivered either way."""
    stays = nights_not_open_for_booking(tool_calls)
    if not stays:
        return None
    try:
        escalation_id = open_escalation(
            conn,
            conversation_id=conversation_id,
            reason=REASON_DATES_NOT_OPEN_FOR_BOOKING,
            notes={"stays": stays},
        )
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "dates_not_open_escalation_failed",
                    "conversation_id": conversation_id,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            ),
            exc_info=exc,
        )
        return None
    logger.info(
        json.dumps(
            {
                "event": "dates_not_open_escalated",
                "conversation_id": conversation_id,
                "escalation_id": escalation_id,
                "night_count": sum(len(stay["nights"]) for stay in stays),
            }
        )
    )
    return escalation_id
