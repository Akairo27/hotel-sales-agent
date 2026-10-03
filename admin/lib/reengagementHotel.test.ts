import { describe, expect, it } from "vitest";
import { reengagementHotelId } from "./reengagementHotel";
import type { EscalationRow } from "./types";

function escalation(overrides: Partial<EscalationRow>): EscalationRow {
  return {
    id: 1,
    conversation_id: 5,
    customer_phone: "966500000000",
    reason: "dates_not_open_for_booking",
    notes: null,
    quote_id: null,
    opened_at: "2026-10-02T10:00:00Z",
    responded_at: null,
    resolved_at: null,
    assigned_to: null,
    ...overrides,
  };
}

const stays = (hotelId: number) =>
  JSON.stringify({ stays: [{ hotel_id: hotelId, room_type_id: 1, nights: ["2026-10-20"] }] });

describe("reengagementHotelId", () => {
  it("is the latest quote's hotel, whichever escalations exist", () => {
    const quotes = [
      { id: 1, hotel_id: 10, created_at: "2026-10-02T10:00:00Z" },
      { id: 2, hotel_id: 20, created_at: "2026-10-02T11:00:00Z" },
    ];
    expect(reengagementHotelId(quotes, [escalation({ notes: stays(30) })])).toBe(20);
  });

  it("breaks a tie on the quote's time by the higher id", () => {
    const at = "2026-10-02T10:00:00Z";
    const quotes = [
      { id: 7, hotel_id: 70, created_at: at },
      { id: 8, hotel_id: 80, created_at: at },
    ];
    expect(reengagementHotelId(quotes, [])).toBe(80);
  });

  it("falls back to the hotel an escalation's stays name, open ones first", () => {
    const rows = [
      escalation({ id: 1, notes: stays(30), resolved_at: "2026-10-02T11:00:00Z" }),
      escalation({ id: 2, notes: stays(40) }),
    ];
    expect(reengagementHotelId([], rows)).toBe(40);
  });

  it("skips escalations that name no hotel, and malformed notes", () => {
    const rows = [
      escalation({ id: 1, reason: "booking_requested", notes: null }),
      escalation({ id: 2, notes: "not json" }),
      escalation({ id: 3, notes: stays(50) }),
    ];
    expect(reengagementHotelId([], rows)).toBe(50);
  });

  it("is null when nothing names a hotel (the variant with no hotel name)", () => {
    expect(reengagementHotelId([], [escalation({ reason: "unsupported_message_type" })])).toBeNull();
    expect(reengagementHotelId([], [])).toBeNull();
  });
});
