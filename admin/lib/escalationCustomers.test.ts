import { describe, expect, it } from "vitest";
import {
  type CustomerTakeover,
  REASON_PRIORITY,
  customerPage,
  customerPageFor,
  groupByCustomer,
  orderEscalations,
} from "./escalationCustomers";
import type { EscalationRow } from "./types";

const ME = "user-me";
const NOBODY = new Map<number, CustomerTakeover>();

function heldBy(holderId: string, takenOverAt: string): CustomerTakeover {
  return { holderId, holderName: `Staff ${holderId}`, takenOverAt };
}

function escalation(overrides: Partial<EscalationRow> & { id: number }): EscalationRow {
  return {
    conversation_id: 1,
    customer_phone: "+966500001469",
    reason: "delivery_failed",
    notes: null,
    quote_id: null,
    opened_at: "2026-10-01T06:00:00Z",
    responded_at: null,
    resolved_at: null,
    assigned_to: null,
    ...overrides,
  };
}

describe("REASON_PRIORITY", () => {
  it("is the owner-approved order, booking requests first", () => {
    expect(REASON_PRIORITY).toEqual([
      "booking",
      "dates_not_open",
      "guard",
      "failure",
      "unanswered",
      "caps",
      "media",
    ]);
  });
});

describe("orderEscalations", () => {
  it("puts open ones first by importance then age, closed ones last newest first", () => {
    const ordered = orderEscalations([
      escalation({ id: 1, reason: "delivery_failed", opened_at: "2026-10-01T05:00:00Z" }),
      escalation({ id: 2, reason: "booking_requested", opened_at: "2026-10-01T07:00:00Z" }),
      escalation({ id: 3, reason: "booking_requested", opened_at: "2026-10-01T06:00:00Z" }),
      escalation({ id: 4, reason: "booking_requested", resolved_at: "2026-10-01T08:00:00Z", opened_at: "2026-10-01T04:00:00Z" }),
      escalation({ id: 5, reason: "unsupported_message_type", resolved_at: "2026-10-01T08:00:00Z", opened_at: "2026-10-01T05:30:00Z" }),
    ]);
    expect(ordered.map((row) => row.id)).toEqual([3, 2, 1, 5, 4]);
  });
});

describe("groupByCustomer", () => {
  it("summarises each customer: open count, reasons by importance, oldest unhandled", () => {
    const [customer] = groupByCustomer(
      [
        escalation({ id: 1, reason: "turn_cap_exceeded", opened_at: "2026-10-01T05:00:00Z" }),
        escalation({ id: 2, reason: "booking_requested", opened_at: "2026-10-01T07:00:00Z" }),
        escalation({ id: 3, reason: "unsupported_message_type", resolved_at: "2026-10-01T08:00:00Z" }),
      ],
      NOBODY,
      ME,
    );
    expect(customer.conversationId).toBe(1);
    expect(customer.openCount).toBe(2);
    expect(customer.openGroups).toEqual(["booking", "caps"]);
    expect(customer.allGroups).toEqual(["booking", "caps", "media"]);
    expect(customer.oldestUnhandledAt).toBe("2026-10-01T05:00:00Z");
    expect(customer.takeover).toBeNull();
    expect(customer.escalations.map((row) => row.id)).toEqual([2, 1, 3]);
  });

  it("sorts customers with an open booking request first, then the oldest unhandled", () => {
    const customers = groupByCustomer(
      [
        escalation({ id: 1, conversation_id: 10, opened_at: "2026-10-01T01:00:00Z" }),
        escalation({ id: 2, conversation_id: 20, reason: "booking_requested", opened_at: "2026-10-01T09:00:00Z" }),
        escalation({ id: 3, conversation_id: 30, opened_at: "2026-10-01T03:00:00Z" }),
        escalation({ id: 4, conversation_id: 40, reason: "booking_requested", opened_at: "2026-10-01T08:00:00Z" }),
        escalation({ id: 5, conversation_id: 50, resolved_at: "2026-10-01T09:30:00Z", opened_at: "2026-10-01T09:00:00Z" }),
      ],
      NOBODY,
      ME,
    );
    expect(customers.map((customer) => customer.conversationId)).toEqual([40, 20, 10, 30, 50]);
  });

  it("puts customers someone holds after the ones nobody holds, even a booking request", () => {
    const customers = groupByCustomer(
      [
        escalation({ id: 1, conversation_id: 10, reason: "booking_requested", opened_at: "2026-10-01T01:00:00Z" }),
        escalation({ id: 2, conversation_id: 20, opened_at: "2026-10-01T05:00:00Z" }),
      ],
      new Map([[10, heldBy("someone", "2026-10-01T02:00:00Z")]]),
      ME,
    );
    expect(
      customers.map((customer) => [customer.conversationId, customer.oldestUnhandledAt, customer.takeover?.holderId]),
    ).toEqual([
      [20, "2026-10-01T05:00:00Z", undefined],
      [10, null, "someone"],
    ]);
  });

  it("lists the customers the current user holds before others', then the longest held", () => {
    const customers = groupByCustomer(
      [
        escalation({ id: 1, conversation_id: 10 }),
        escalation({ id: 2, conversation_id: 20 }),
        escalation({ id: 3, conversation_id: 30 }),
      ],
      new Map([
        [10, heldBy("someone", "2026-10-01T03:00:00Z")],
        [20, heldBy(ME, "2026-10-01T04:00:00Z")],
        [30, heldBy("someone", "2026-10-01T02:00:00Z")],
      ]),
      ME,
    );
    expect(customers.map((customer) => customer.conversationId)).toEqual([20, 30, 10]);
  });

  it("files a reason it does not know under failure, so it still shows", () => {
    const [customer] = groupByCustomer(
      [escalation({ id: 1, reason: "a_reason_added_later" })],
      NOBODY,
      ME,
    );
    expect(customer.openGroups).toEqual(["failure"]);
  });
});

describe("customer page links", () => {
  it("point at the customer's page, at one escalation when given", () => {
    expect(customerPage(7)).toBe("/escalations/conversations/7");
    expect(customerPageFor(7, 19)).toBe("/escalations/conversations/7#escalation-19");
  });
});
