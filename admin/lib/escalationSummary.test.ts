import { describe, expect, it } from "vitest";
import { NO_OPEN_ESCALATIONS, escalationSummary, formatStayRange } from "./escalationSummary";
import type { EscalationRow, QuoteSummaryRow } from "./types";

const NOW = new Date("2026-10-02T12:00:00Z");
const NAMES = { hotel: (id: number) => (id === 2 ? "فندق الاختبار2" : `الفندق ${id}`) };

function escalation(overrides: Partial<EscalationRow>): EscalationRow {
  return {
    id: 1,
    conversation_id: 5,
    customer_phone: "966500000000",
    reason: "booking_requested",
    notes: null,
    quote_id: null,
    opened_at: "2026-10-02T10:00:00Z",
    responded_at: null,
    resolved_at: null,
    assigned_to: null,
    ...overrides,
  };
}

const QUOTE: QuoteSummaryRow = {
  id: 11,
  hotel_id: 2,
  room_type_id: 3,
  check_in: "2026-10-20",
  check_out: "2026-10-22",
  rooms: 1,
  ask_price_total: 65000,
  created_at: "2026-10-02T11:00:00Z",
};

describe("formatStayRange", () => {
  it("writes a stay within a month as one range", () => {
    expect(formatStayRange("2026-10-20", "2026-10-22", NOW)).toBe("20–22 أكتوبر");
  });

  it("writes a stay across months with both dates", () => {
    expect(formatStayRange("2026-10-30", "2026-11-02", NOW)).toBe("30 أكتوبر – 2 نوفمبر");
  });

  it("adds the year when it is not this year", () => {
    expect(formatStayRange("2027-01-05", "2027-01-08", NOW)).toBe("5–8 يناير 2027");
    expect(formatStayRange("2026-12-30", "2027-01-02", NOW)).toBe("30 ديسمبر – 2 يناير 2027");
  });

  it("shows what is not a date as it is", () => {
    expect(formatStayRange("soon", "later", NOW)).toBe("soon – later");
  });
});

describe("escalationSummary", () => {
  it("names the hotel and stay of a booking request", () => {
    expect(
      escalationSummary([escalation({ quote_id: 11 })], [QUOTE], NAMES, NOW),
    ).toBe("طلب حجز: فندق الاختبار2، 20–22 أكتوبر");
  });

  it("falls back to the reason when the booking's quote is not there", () => {
    expect(escalationSummary([escalation({ quote_id: 99 })], [QUOTE], NAMES, NOW)).toBe("طلب حجز");
  });

  it("names a voice message, and other media by their type", () => {
    const media = (type: string) =>
      escalation({ reason: "unsupported_message_type", notes: JSON.stringify({ message_type: type }) });
    expect(escalationSummary([media("audio")], [], NAMES, NOW)).toBe("رسالة صوتية");
    expect(escalationSummary([media("image")], [], NAMES, NOW)).toBe("صورة");
    expect(escalationSummary([media("hologram")], [], NAMES, NOW)).toBe("رسالة وسائط");
    expect(
      escalationSummary([escalation({ reason: "unsupported_message_type" })], [], NAMES, NOW),
    ).toBe("رسالة وسائط");
  });

  it("names the hotels whose dates are not open", () => {
    const notes = JSON.stringify({
      stays: [
        { hotel_id: 2, room_type_id: 3, nights: ["2026-10-20"] },
        { hotel_id: 2, room_type_id: 4, nights: ["2026-10-21"] },
        { hotel_id: 6, room_type_id: 1, nights: ["2026-10-22"] },
      ],
    });
    expect(
      escalationSummary(
        [escalation({ reason: "dates_not_open_for_booking", notes })],
        [],
        NAMES,
        NOW,
      ),
    ).toBe("تواريخ لم يُفتح حجزها: فندق الاختبار2، الفندق 6");
    expect(
      escalationSummary([escalation({ reason: "dates_not_open_for_booking" })], [], NAMES, NOW),
    ).toBe("تواريخ لم يُفتح حجزها");
  });

  it("uses the group's label for the other reasons", () => {
    expect(
      escalationSummary([escalation({ reason: "output_guard_violation_price" })], [], NAMES, NOW),
    ).toBe("رد محجوب للمراجعة");
    expect(
      escalationSummary([escalation({ reason: "turn_cap_exceeded" })], [], NAMES, NOW),
    ).toBe("تجاوز حد الاستخدام");
  });

  it("puts the most important open reason first and skips closed ones", () => {
    const rows = [
      escalation({ id: 1, reason: "unanswered_at_startup" }),
      escalation({ id: 2, quote_id: 11 }),
      escalation({ id: 3, reason: "turn_cap_exceeded", resolved_at: "2026-10-02T11:00:00Z" }),
    ];
    expect(escalationSummary(rows, [QUOTE], NAMES, NOW)).toBe(
      "طلب حجز: فندق الاختبار2، 20–22 أكتوبر · رسالة لم يُرد عليها",
    );
  });

  it("says each different reason once, and counts the rest", () => {
    const rows = [
      escalation({ id: 1, quote_id: 11 }),
      escalation({ id: 2, quote_id: 11 }),
      escalation({ id: 3, reason: "unanswered_at_startup" }),
      escalation({ id: 4, reason: "turn_cap_exceeded" }),
    ];
    expect(escalationSummary(rows.slice(0, 2), [QUOTE], NAMES, NOW)).toBe(
      "طلب حجز: فندق الاختبار2، 20–22 أكتوبر",
    );
    expect(escalationSummary(rows, [QUOTE], NAMES, NOW)).toBe(
      "طلب حجز: فندق الاختبار2، 20–22 أكتوبر · رسالة لم يُرد عليها · +1",
    );
  });

  it("says so when everything is closed", () => {
    expect(
      escalationSummary([escalation({ resolved_at: "2026-10-02T11:00:00Z" })], [], NAMES, NOW),
    ).toBe(NO_OPEN_ESCALATIONS);
  });
});
