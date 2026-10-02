import { describe, expect, it } from "vitest";
import {
  QUOTE_VALIDITY_MINUTES,
  quoteReplyText,
  quoteValidUntil,
  quoteValidity,
} from "./quoteStatus";

const CREATED = "2026-10-02T11:00:00Z";
const MS_PER_MINUTE = 60_000;

describe("quoteValidity", () => {
  const until = Date.parse(CREATED) + QUOTE_VALIDITY_MINUTES * MS_PER_MINUTE;

  it("is valid until the validity ends, and expired from that instant", () => {
    expect(quoteValidUntil(CREATED)).toBe(until);
    expect(quoteValidity(CREATED, new Date(until - 1))).toBe("valid");
    expect(quoteValidity(CREATED, new Date(until))).toBe("expired");
    expect(quoteValidity(CREATED, new Date(until + MS_PER_MINUTE))).toBe("expired");
  });
});

describe("quoteReplyText", () => {
  const quote = {
    check_in: "2026-10-20",
    check_out: "2026-10-22",
    rooms: 1,
    ask_price_total: 65000,
  };

  it("writes the stored quote briefly, its own total in riyals", () => {
    const text = quoteReplyText(
      quote,
      { hotelName: "فندق الاختبار2", roomTypeName: "جناح ملكي" },
      new Date("2026-10-02T12:00:00Z"),
    );
    expect(text).toBe(
      "فندق الاختبار2، جناح ملكي، من 20 أكتوبر إلى 22 أكتوبر، عدد الغرف 1، الإجمالي 650.00 ريال.",
    );
  });
});
