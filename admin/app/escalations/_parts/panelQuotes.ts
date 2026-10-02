import { escalationGroup, formatStayDate } from "@/lib/escalations";
import { formatHalalasAsRiyal } from "@/lib/money";
import { quoteReplyText } from "@/lib/quoteStatus";
import type { EscalationRow, QuoteSummaryRow } from "@/lib/types";
import type { PanelQuote } from "../conversations/[conversationId]/QuotePanel";
import type { StayNames } from "./names";

/** The conversation's quotes as the side panel shows them: names resolved,
 * the details line and the reply-box text written, and whether the customer
 * asked to book it (an escalation for a booking request names the quote). */
export function buildPanelQuotes(
  quotes: readonly QuoteSummaryRow[],
  escalations: readonly EscalationRow[],
  names: StayNames,
  now: Date,
): PanelQuote[] {
  const bookingRequested = new Set(
    escalations
      .filter((escalation) => escalationGroup(escalation.reason) === "booking")
      .flatMap((escalation) => (escalation.quote_id === null ? [] : [escalation.quote_id])),
  );
  return quotes.map((quote) => {
    const hotelName = names.hotels.get(quote.hotel_id) ?? `الفندق ${quote.hotel_id}`;
    const roomTypeName = names.roomTypes.get(quote.room_type_id) ?? `نوع الغرفة ${quote.room_type_id}`;
    return {
      id: quote.id,
      title: `${hotelName}، ${roomTypeName}`,
      details:
        `العرض رقم ${quote.id} · من ${formatStayDate(quote.check_in, now)} إلى ` +
        `${formatStayDate(quote.check_out, now)} · عدد الغرف ${quote.rooms} · ` +
        `الإجمالي ${formatHalalasAsRiyal(quote.ask_price_total)}`,
      createdAt: quote.created_at,
      bookingRequested: bookingRequested.has(quote.id),
      replyText: quoteReplyText(quote, { hotelName, roomTypeName }, now),
    };
  });
}
