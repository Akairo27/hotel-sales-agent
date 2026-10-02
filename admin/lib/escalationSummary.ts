// The one line pinned under the customer page's header (owner request
// 2026-10-02): why the bot stopped and what the customer is waiting for,
// from the open escalations -- «طلب حجز: فندق الاختبار2، 20–22 أكتوبر» or
// «رسالة صوتية». Pure.
import { isOpen, orderEscalations } from "@/lib/escalationCustomers";
import {
  ESCALATION_GROUP_LABELS,
  escalationGroup,
  formatStayDate,
  notOpenStays,
  parseNotes,
} from "@/lib/escalations";
import type { EscalationRow, QuoteSummaryRow } from "@/lib/types";

export const NO_OPEN_ESCALATIONS = "كل التصعيدات مغلقة.";

// How many different reasons the line spells out before «+n».
const MAX_SUMMARY_PARTS = 2;
const SUMMARY_SEPARATOR = " · ";

// WhatsApp's message types for the media escalation, in the words staff use.
const MEDIA_LABELS: Record<string, string> = {
  audio: "رسالة صوتية",
  image: "صورة",
  video: "فيديو",
  document: "ملف",
  sticker: "ملصق",
  location: "موقع",
};

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

/** A stay as the bot writes it: «20–22 أكتوبر» within a month, otherwise
 * both dates in full («30 أكتوبر – 2 نوفمبر»). Anything that is not an ISO
 * date is shown as it is. */
export function formatStayRange(checkIn: string, checkOut: string, now: Date): string {
  const from = ISO_DATE.exec(checkIn);
  const to = ISO_DATE.exec(checkOut);
  if (from && to && from[1] === to[1] && from[2] === to[2]) {
    const tail = formatStayDate(checkOut, now).replace(/^\d+/, "");
    return `${Number(from[3])}–${Number(to[3])}${tail}`;
  }
  return `${formatStayDate(checkIn, now)} – ${formatStayDate(checkOut, now)}`;
}

export interface SummaryNames {
  hotel(id: number): string;
}

function mediaLabel(notes: Record<string, unknown>): string {
  const type = notes.message_type;
  return (typeof type === "string" ? MEDIA_LABELS[type] : undefined) ?? ESCALATION_GROUP_LABELS.media;
}

function partFor(
  escalation: EscalationRow,
  quotes: readonly QuoteSummaryRow[],
  names: SummaryNames,
  now: Date,
): string {
  const group = escalationGroup(escalation.reason);
  const label = ESCALATION_GROUP_LABELS[group];
  const notes = parseNotes(escalation.notes);
  if (group === "booking") {
    const quote = quotes.find((candidate) => candidate.id === escalation.quote_id);
    return quote
      ? `${label}: ${names.hotel(quote.hotel_id)}، ${formatStayRange(quote.check_in, quote.check_out, now)}`
      : label;
  }
  if (group === "dates_not_open") {
    const hotels = [...new Set(notOpenStays(notes).map((stay) => names.hotel(stay.hotelId)))];
    return hotels.length > 0 ? `${label}: ${hotels.join("، ")}` : label;
  }
  return group === "media" ? mediaLabel(notes) : label;
}

/** The summary of the open escalations, most important reason first, each
 * different reason once, the rest as «+n»; or NO_OPEN_ESCALATIONS. */
export function escalationSummary(
  escalations: readonly EscalationRow[],
  quotes: readonly QuoteSummaryRow[],
  names: SummaryNames,
  now: Date,
): string {
  const parts = [
    ...new Set(
      orderEscalations(escalations)
        .filter(isOpen)
        .map((escalation) => partFor(escalation, quotes, names, now)),
    ),
  ];
  if (parts.length === 0) {
    return NO_OPEN_ESCALATIONS;
  }
  const shown = parts.slice(0, MAX_SUMMARY_PARTS).join(SUMMARY_SEPARATOR);
  const hidden = parts.length - MAX_SUMMARY_PARTS;
  return hidden > 0 ? `${shown}${SUMMARY_SEPARATOR}+${hidden}` : shown;
}
