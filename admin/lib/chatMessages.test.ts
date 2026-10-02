import { describe, expect, it } from "vitest";
import { BUBBLE_CLASSES, SENDER_LABEL_CLASSES, messageSender, senderLabel } from "./chatMessages";

const CUSTOMER = { direction: "inbound", staff_reply_id: null } as const;
const BOT = { direction: "outbound", staff_reply_id: null } as const;
const STAFF = { direction: "outbound", staff_reply_id: 7 } as const;

describe("messageSender", () => {
  it("tells the customer, the bot and a staff member apart", () => {
    expect(messageSender(CUSTOMER)).toBe("customer");
    expect(messageSender(BOT)).toBe("bot");
    expect(messageSender(STAFF)).toBe("staff");
  });
});

describe("senderLabel", () => {
  const names = new Map([[7, "Sara"]]);

  it("labels each sender", () => {
    expect(senderLabel(CUSTOMER, names)).toBe("العميل");
    expect(senderLabel(BOT, names)).toBe("الوكيل");
    expect(senderLabel(STAFF, names)).toBe("الموظف: Sara");
  });

  it("names the role alone when the author's name could not be read", () => {
    expect(senderLabel({ direction: "outbound", staff_reply_id: 8 }, names)).toBe("الموظف");
  });
});

describe("bubble styles", () => {
  it("gives each sender its own colours, so a glance tells them apart", () => {
    const senders = ["customer", "bot", "staff"] as const;
    expect(new Set(senders.map((sender) => BUBBLE_CLASSES[sender])).size).toBe(3);
    expect(new Set(senders.map((sender) => SENDER_LABEL_CLASSES[sender])).size).toBe(3);
  });

  it("keeps the customer on the start edge and replies on the end edge", () => {
    expect(BUBBLE_CLASSES.customer).toContain("me-");
    expect(BUBBLE_CLASSES.bot).toContain("ms-");
    expect(BUBBLE_CLASSES.staff).toContain("ms-");
  });
});
