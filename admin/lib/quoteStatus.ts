// A quote's status badge and its short text for the reply box (customer
// page, owner request 2026-10-02). Pure: the clock comes in as `now`.
//
// The validity mirrors the agent's QUOTE_VALIDITY_MINUTES default (services/
// agent/llm/config.py): the dashboard cannot read the agent's settings, so
// a changed setting there must be changed here too. It only decides which
// badge a quote wears and whether the quote may be pasted into a reply; the
// output guard's own window still governs what the bot may repeat.
import { formatStayDate } from "@/lib/escalations";
import { formatHalalasAsRiyal } from "@/lib/money";
import type { QuoteSummaryRow } from "@/lib/types";

export const QUOTE_VALIDITY_MINUTES = 30;
const MS_PER_MINUTE = 60_000;

export type QuoteValidity = "valid" | "expired";

/** The instant a quote stops being valid. */
export function quoteValidUntil(createdAt: string): number {
  return Date.parse(createdAt) + QUOTE_VALIDITY_MINUTES * MS_PER_MINUTE;
}

/** Whether a quote can still be repeated to the customer. Whether the
 * customer asked to book it is a separate fact: such a quote keeps that
 * badge when it expires, and shows both while it is still valid. */
export function quoteValidity(createdAt: string, now: Date): QuoteValidity {
  return now.getTime() < quoteValidUntil(createdAt) ? "valid" : "expired";
}

export interface QuoteText {
  hotelName: string;
  roomTypeName: string;
}

/** The quote as a short text staff can send, from the stored quote alone
 * (its own total, never computed here). Staff edit it before sending. */
export function quoteReplyText(
  quote: Pick<QuoteSummaryRow, "check_in" | "check_out" | "rooms" | "ask_price_total">,
  text: QuoteText,
  now: Date,
): string {
  return (
    `${text.hotelName}، ${text.roomTypeName}، ` +
    `من ${formatStayDate(quote.check_in, now)} إلى ${formatStayDate(quote.check_out, now)}، ` +
    `عدد الغرف ${quote.rooms}، الإجمالي ${formatHalalasAsRiyal(quote.ask_price_total)}.`
  );
}
