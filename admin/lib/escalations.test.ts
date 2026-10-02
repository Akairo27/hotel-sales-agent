import { describe, expect, it } from "vitest";
import {
  ESCALATION_GROUP_LABELS,
  escalationGroup,
  escalationLabel,
  formatAge,
  formatRiyadhDateTime,
  formatStayDate,
  halalasList,
  maskPhone,
  notOpenStays,
  parseNotes,
  stringList,
  technicalNotes,
} from "./escalations";

// Every reason the agent writes today (services/agent), and its group.
const REASONS: Array<[string, string]> = [
  ["booking_requested", "booking"],
  ["dates_not_open_for_booking", "dates_not_open"],
  ["output_guard_violation_mismatch", "guard"],
  ["output_guard_violation_unparseable", "guard"],
  ["output_guard_violation_foreign_currency", "guard"],
  ["output_guard_violation_missing_currency", "guard"],
  ["output_guard_violation_percentage_stated", "guard"],
  ["output_guard_violation_booking_claim", "guard"],
  ["unsupported_message_type", "media"],
  ["turn_cap_exceeded", "caps"],
  ["token_spend_cap_exceeded", "caps"],
  ["number_daily_token_cap_exceeded", "caps"],
  ["daily_spend_cap_exceeded", "caps"],
  ["message_rate_cap_exceeded", "caps"],
  ["unanswered_at_startup", "unanswered"],
  ["model_unavailable", "failure"],
  ["turn_budget_exceeded", "failure"],
  ["usage_unavailable", "failure"],
  ["tool_loop_limit_exceeded", "failure"],
  ["unknown_tool", "failure"],
  ["pricing_error", "failure"],
  ["empty_reply", "failure"],
  ["reply_too_long", "failure"],
  ["delivery_failed", "failure"],
  ["internal_error", "failure"],
  ["booking_button_mismatch", "failure"],
];

describe("escalationGroup", () => {
  it.each(REASONS)("puts %s in %s", (reason, group) => {
    expect(escalationGroup(reason)).toBe(group);
  });

  it("shows a reason it does not know as a failure, never hides it", () => {
    expect(escalationGroup("a_reason_added_later")).toBe("failure");
  });
});

describe("escalationLabel", () => {
  it("uses the owner-approved labels", () => {
    expect(escalationLabel("booking_requested")).toBe("طلب حجز");
    expect(escalationLabel("dates_not_open_for_booking")).toBe("تواريخ لم يُفتح حجزها");
    expect(escalationLabel("output_guard_violation_booking_claim")).toBe("رد محجوب للمراجعة");
    expect(escalationLabel("unsupported_message_type")).toBe("رسالة وسائط");
    expect(escalationLabel("turn_cap_exceeded")).toBe("تجاوز حد الاستخدام");
    expect(escalationLabel("unanswered_at_startup")).toBe("رسالة لم يُرد عليها");
    expect(escalationLabel("delivery_failed")).toBe("تعذّر الرد تلقائياً");
    expect(Object.keys(ESCALATION_GROUP_LABELS)).toHaveLength(7);
  });
});

describe("maskPhone", () => {
  it("keeps only the last four digits", () => {
    expect(maskPhone("+966500001469")).toBe("***1469");
  });
});

describe("formatRiyadhDateTime", () => {
  it("renders the instant in Riyadh time with Western digits", () => {
    expect(formatRiyadhDateTime("2026-10-01T05:50:46.12+00:00")).toBe("2026-10-01 08:50");
    expect(formatRiyadhDateTime("2026-09-30T21:30:00Z")).toBe("2026-10-01 00:30");
  });
});

describe("formatStayDate", () => {
  // 2026-10-02 08:00 in Riyadh.
  const now = new Date("2026-10-02T05:00:00Z");

  it("writes a stay date as the bot does: day and Arabic month, Western digits", () => {
    expect(formatStayDate("2026-10-20", now)).toBe("20 أكتوبر");
    expect(formatStayDate("2026-12-01", now)).toBe("1 ديسمبر");
  });

  it("adds the year only when it is not the current year in Riyadh", () => {
    expect(formatStayDate("2027-01-05", now)).toBe("5 يناير 2027");
    // 31 Dec 22:00 UTC is already 1 January in Riyadh.
    expect(formatStayDate("2027-01-05", new Date("2026-12-31T22:00:00Z"))).toBe("5 يناير");
  });

  it("shows anything that is not a date as it is, never hides it", () => {
    expect(formatStayDate("next week", now)).toBe("next week");
    expect(formatStayDate("2026-10-20T00:00:00Z", now)).toBe("2026-10-20T00:00:00Z");
  });
});

describe("formatAge", () => {
  const opened = "2026-10-01T05:00:00Z";
  const after = (minutes: number) => new Date(Date.parse(opened) + minutes * 60_000);

  it.each([
    [0, "الآن"],
    [1, "منذ دقيقة"],
    [2, "منذ دقيقتين"],
    [5, "منذ 5 دقائق"],
    [10, "منذ 10 دقائق"],
    [11, "منذ 11 دقيقة"],
    [60, "منذ ساعة"],
    [120, "منذ ساعتين"],
    [300, "منذ 5 ساعات"],
    [23 * 60, "منذ 23 ساعة"],
    [24 * 60, "منذ يوم"],
    [48 * 60, "منذ يومين"],
    [3 * 24 * 60, "منذ 3 أيام"],
    [12 * 24 * 60, "منذ 12 يوماً"],
    [109, "منذ ساعة و49 دقيقة"],
    [121, "منذ ساعتين ودقيقة"],
    [122, "منذ ساعتين ودقيقتين"],
    [185, "منذ 3 ساعات و5 دقائق"],
    [11 * 60 + 30, "منذ 11 ساعة و30 دقيقة"],
    [28 * 60, "منذ يوم و4 ساعات"],
    [49 * 60, "منذ يومين وساعة"],
    [3 * 24 * 60 + 2 * 60, "منذ 3 أيام وساعتين"],
  ])("after %i minutes says %s", (minutes, expected) => {
    expect(formatAge(opened, after(minutes))).toBe(expected);
  });

  it("never rounds a part away (owner report: 09:41 at 11:30 read «منذ ساعة»)", () => {
    expect(formatAge("2026-10-01T06:41:00Z", new Date("2026-10-01T08:30:00Z"))).toBe(
      "منذ ساعة و49 دقيقة",
    );
  });
});

describe("parseNotes", () => {
  it("reads the JSON object the agent writes", () => {
    expect(parseNotes('{"message_type": "video"}')).toEqual({ message_type: "video" });
  });

  it.each([null, "", "not json", "[1, 2]", "null", '"text"'])(
    "treats %j as no notes",
    (notes) => {
      expect(parseNotes(notes)).toEqual({});
    },
  );
});

describe("notOpenStays", () => {
  it("reads the stays staff_follow_up.py writes and skips malformed ones", () => {
    const notes = parseNotes(
      JSON.stringify({
        stays: [
          { hotel_id: 1, room_type_id: 2, nights: ["2026-10-20", 7, "2026-10-21"] },
          { hotel_id: "1", room_type_id: 2, nights: [] },
          null,
        ],
      }),
    );
    expect(notOpenStays(notes)).toEqual([
      { hotelId: 1, roomTypeId: 2, nights: ["2026-10-20", "2026-10-21"] },
    ]);
    expect(notOpenStays({})).toEqual([]);
  });
});

describe("technicalNotes", () => {
  it("leaves out what the page shows on its own and the retention reminder", () => {
    const notes = {
      exception_type: "ModelUnavailableError",
      quote_ids: [11],
      blocked_reply_text: "shown on its own",
      retention: "internal reminder",
    };
    expect(technicalNotes(notes)).toEqual([
      ["exception_type", "ModelUnavailableError"],
      ["quote_ids", "[11]"],
    ]);
  });
});

describe("stringList and halalasList", () => {
  it("keep only the values of the right kind", () => {
    expect(stringList(["passed your request", 3])).toEqual(["passed your request"]);
    expect(stringList("not a list")).toEqual([]);
    expect(halalasList([90_000, -1, 1.5, "7"])).toEqual([90_000]);
  });
});
