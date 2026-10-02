import { describe, expect, it } from "vitest";
import type { EscalationRow, QuoteSummaryRow } from "@/lib/types";
import { buildPanelQuotes } from "./panelQuotes";

const NOW = new Date("2026-10-02T12:00:00Z");
const NAMES = {
  hotels: new Map([[2, "فندق الاختبار2"]]),
  roomTypes: new Map([[3, "جناح ملكي"]]),
};

function quote(id: number, overrides: Partial<QuoteSummaryRow> = {}): QuoteSummaryRow {
  return {
    id,
    hotel_id: 2,
    room_type_id: 3,
    check_in: "2026-10-20",
    check_out: "2026-10-22",
    rooms: 1,
    ask_price_total: 65000,
    created_at: "2026-10-02T11:00:00Z",
    ...overrides,
  };
}

function escalation(reason: string, quoteId: number | null): EscalationRow {
  return {
    id: 1,
    conversation_id: 5,
    customer_phone: "966500000000",
    reason,
    notes: null,
    quote_id: quoteId,
    opened_at: "2026-10-02T11:30:00Z",
    responded_at: null,
    resolved_at: null,
    assigned_to: null,
  };
}

describe("buildPanelQuotes", () => {
  it("writes the title, the details and the text for the reply box", () => {
    const [panel] = buildPanelQuotes([quote(11)], [], NAMES, NOW);
    expect(panel.title).toBe("فندق الاختبار2، جناح ملكي");
    expect(panel.details).toContain("العرض رقم 11");
    expect(panel.details).toContain("650.00 ريال");
    expect(panel.replyText).toBe(
      "فندق الاختبار2، جناح ملكي، من 20 أكتوبر إلى 22 أكتوبر، عدد الغرف 1، الإجمالي 650.00 ريال.",
    );
    expect(panel.createdAt).toBe("2026-10-02T11:00:00Z");
  });

  it("marks only the quotes a booking request names", () => {
    const panels = buildPanelQuotes(
      [quote(11), quote(12), quote(13)],
      [escalation("booking_requested", 11), escalation("unanswered_at_startup", 12)],
      NAMES,
      NOW,
    );
    expect(panels.map((panel) => panel.bookingRequested)).toEqual([true, false, false]);
  });

  it("names a hotel or room type it could not read by its id", () => {
    const [panel] = buildPanelQuotes([quote(11, { hotel_id: 9, room_type_id: 8 })], [], NAMES, NOW);
    expect(panel.title).toBe("الفندق 9، نوع الغرفة 8");
  });
});
