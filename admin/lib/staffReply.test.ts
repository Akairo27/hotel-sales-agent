import { describe, expect, it } from "vitest";
import {
  CUSTOMER_SERVICE_WINDOW_MS,
  MAX_REPLY_LENGTH,
  SENDING_STALE_MS,
  isWithinCustomerServiceWindow,
  replyDraftProblem,
  replyProblemHint,
  replyState,
  replyStateLabel,
  replyStatusFromBody,
  sendResultLabel,
  staffMessageLabel,
  templateButtonProblem,
  TEMPLATE_NOT_ENABLED,
  windowRemainingMs,
} from "./staffReply";

const NOW = new Date("2026-10-02T12:00:00Z");
const UNCLAIMED = { claimed_at: null, sent_at: null, failed_at: null, failure_reason: null };
const AT = "2026-10-02T11:59:00Z";

describe("isWithinCustomerServiceWindow", () => {
  it("is open for a message less than 24 hours old", () => {
    const justInside = new Date(NOW.getTime() - CUSTOMER_SERVICE_WINDOW_MS + 1).toISOString();
    expect(isWithinCustomerServiceWindow(justInside, NOW)).toBe(true);
    expect(isWithinCustomerServiceWindow(NOW.toISOString(), NOW)).toBe(true);
  });

  it("is closed at exactly 24 hours, as the agent's strict comparison", () => {
    const exactly = new Date(NOW.getTime() - CUSTOMER_SERVICE_WINDOW_MS).toISOString();
    expect(isWithinCustomerServiceWindow(exactly, NOW)).toBe(false);
    expect(isWithinCustomerServiceWindow("2026-09-30T00:00:00Z", NOW)).toBe(false);
  });

  it("is closed when the customer never wrote", () => {
    expect(isWithinCustomerServiceWindow(null, NOW)).toBe(false);
  });
});

describe("replyDraftProblem", () => {
  it("accepts text, in any language", () => {
    expect(replyDraftProblem("حياك الله")).toBeNull();
    expect(replyDraftProblem("Hello")).toBeNull();
  });

  it("refuses an empty or whitespace-only draft, newlines included", () => {
    expect(replyDraftProblem("")).not.toBeNull();
    expect(replyDraftProblem("   ")).not.toBeNull();
    expect(replyDraftProblem("\n\n\t")).not.toBeNull();
  });

  it("counts characters, not UTF-16 units, against WhatsApp's limit", () => {
    expect(replyDraftProblem("a".repeat(MAX_REPLY_LENGTH))).toBeNull();
    expect(replyDraftProblem("a".repeat(MAX_REPLY_LENGTH + 1))).not.toBeNull();
    // 4096 emoji are 8192 UTF-16 units but 4096 characters.
    expect(replyDraftProblem("😀".repeat(MAX_REPLY_LENGTH))).toBeNull();
    expect(replyDraftProblem("😀".repeat(MAX_REPLY_LENGTH + 1))).not.toBeNull();
  });
});

describe("replyStatusFromBody", () => {
  it("reads every status services/agent/staff_reply.py answers with", () => {
    for (const status of [
      "sent",
      "outside_window",
      "already_claimed",
      "takeover_ended",
      "not_found",
      "failed",
      "unavailable",
      "window_open",
      "not_configured",
    ] as const) {
      expect(replyStatusFromBody({ status })).toBe(status);
    }
  });

  it("reads anything else as unreachable", () => {
    expect(replyStatusFromBody(null)).toBe("unreachable");
    expect(replyStatusFromBody("sent")).toBe("unreachable");
    expect(replyStatusFromBody({ status: "SENT" })).toBe("unreachable");
    expect(replyStatusFromBody({ detail: "unauthorized" })).toBe("unreachable");
  });

  it("has an Arabic text for every status", () => {
    for (const status of [
      "sent",
      "outside_window",
      "already_claimed",
      "takeover_ended",
      "not_found",
      "failed",
      "unavailable",
      "window_open",
      "not_configured",
      "unreachable",
    ] as const) {
      expect(sendResultLabel(status)).not.toBe("");
    }
  });
});

describe("replyState", () => {
  it("follows the stored columns", () => {
    expect(replyState(UNCLAIMED)).toBe("queued");
    expect(replyState({ ...UNCLAIMED, claimed_at: AT })).toBe("sending");
    expect(replyState({ ...UNCLAIMED, claimed_at: AT, sent_at: AT })).toBe("sent");
    expect(replyState({ ...UNCLAIMED, claimed_at: AT, failed_at: AT })).toBe("failed");
    expect(replyStateLabel("sent")).toBe("أُرسل");
    expect(replyStateLabel("failed")).toBe("لم يُرسل");
  });
});

describe("replyProblemHint", () => {
  it("says nothing for a reply that is sent, queued or just claimed", () => {
    expect(replyProblemHint({ ...UNCLAIMED, claimed_at: AT, sent_at: AT }, NOW)).toBeNull();
    expect(replyProblemHint(UNCLAIMED, NOW)).toBeNull();
    expect(replyProblemHint({ ...UNCLAIMED, claimed_at: AT }, NOW)).toBeNull();
  });

  it("tells staff to call the customer outside the window", () => {
    const hint = replyProblemHint(
      { claimed_at: AT, sent_at: null, failed_at: AT, failure_reason: "outside_window" },
      NOW,
    );
    expect(hint).toContain("24 ساعة");
    expect(hint).toContain("تواصل معه هاتفياً");
  });

  it("warns a failed send may still have arrived, and offers a rewrite", () => {
    const hint = replyProblemHint(
      { claimed_at: AT, sent_at: null, failed_at: AT, failure_reason: "send_failed" },
      NOW,
    );
    expect(hint).toContain("قد يكون الرد وصل العميل");
    expect(hint).toContain("تواصل معه هاتفياً");
  });

  it("never leaves a failure without a hint, whatever the reason", () => {
    expect(
      replyProblemHint({ claimed_at: AT, sent_at: null, failed_at: AT, failure_reason: null }, NOW),
    ).toContain("تواصل معه هاتفياً");
  });

  it("says the outcome is unknown once a claim has had no outcome too long", () => {
    const claimedAt = new Date(NOW.getTime() - SENDING_STALE_MS - 1).toISOString();
    expect(replyProblemHint({ ...UNCLAIMED, claimed_at: claimedAt }, NOW)).toContain(
      "لم يُعرف إن وصل",
    );
    const justClaimed = new Date(NOW.getTime() - SENDING_STALE_MS).toISOString();
    expect(replyProblemHint({ ...UNCLAIMED, claimed_at: justClaimed }, NOW)).toBeNull();
  });
});

describe("staffMessageLabel", () => {
  it("names the staff member", () => {
    expect(staffMessageLabel("Sara")).toBe("الموظف: Sara");
  });

  it("falls back to the role alone when the name could not be read", () => {
    expect(staffMessageLabel(null)).toBe("الموظف");
  });
});

describe("windowRemainingMs", () => {
  it("is what is left of the 24 hours, and 0 once they are over or nobody wrote", () => {
    const eightHoursAgo = new Date(NOW.getTime() - 8 * 60 * 60 * 1000).toISOString();
    expect(windowRemainingMs(eightHoursAgo, NOW)).toBe(16 * 60 * 60 * 1000);
    const exactly = new Date(NOW.getTime() - CUSTOMER_SERVICE_WINDOW_MS).toISOString();
    expect(windowRemainingMs(exactly, NOW)).toBe(0);
    expect(windowRemainingMs("2026-09-30T00:00:00Z", NOW)).toBe(0);
    expect(windowRemainingMs(null, NOW)).toBe(0);
  });
});

describe("templateButtonProblem", () => {
  it("is nothing only when the window is shut and the agent has the template", () => {
    expect(templateButtonProblem(false, true)).toBeNull();
  });

  it("says to write directly while the window is open, whatever the agent has", () => {
    expect(templateButtonProblem(true, true)).toContain("اكتب ردك مباشرة");
    expect(templateButtonProblem(true, false)).toContain("اكتب ردك مباشرة");
  });

  it("says to call the customer while the agent has no template names", () => {
    expect(templateButtonProblem(false, false)).toBe(TEMPLATE_NOT_ENABLED);
    expect(TEMPLATE_NOT_ENABLED).toContain("تواصل مع العميل هاتفياً");
  });
});

describe("a window_open failure", () => {
  it("has its own hint", () => {
    expect(
      replyProblemHint(
        { claimed_at: AT, sent_at: null, failed_at: AT, failure_reason: "window_open" },
        NOW,
      ),
    ).toContain("نافذة الـ24 ساعة مفتوحة");
  });
});
