// Which hotel the re-engagement template names ({{1}}; owner decision
// 2026-10-02): the conversation's latest quote's, else the hotel of the
// first escalation (open ones first, the most important reason first) that
// names one in its notes -- the stays of a dates-not-open escalation -- else
// none, and the agent sends the variant with no hotel name. Pure.
import { orderEscalations } from "@/lib/escalationCustomers";
import { notOpenStays, parseNotes } from "@/lib/escalations";
import type { EscalationRow, QuoteSummaryRow } from "@/lib/types";

export function reengagementHotelId(
  quotes: readonly Pick<QuoteSummaryRow, "id" | "hotel_id" | "created_at">[],
  escalations: readonly EscalationRow[],
): number | null {
  const latestQuote = [...quotes].sort(
    (a, b) => Date.parse(b.created_at) - Date.parse(a.created_at) || b.id - a.id,
  )[0];
  if (latestQuote) {
    return latestQuote.hotel_id;
  }
  for (const escalation of orderEscalations(escalations)) {
    const [stay] = notOpenStays(parseNotes(escalation.notes));
    if (stay) {
      return stay.hotelId;
    }
  }
  return null;
}
