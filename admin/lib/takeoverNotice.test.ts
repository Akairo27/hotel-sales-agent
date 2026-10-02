import { describe, expect, it } from "vitest";
import {
  canRetryNotice,
  noticeResultLabel,
  noticeState,
  noticeStateLabel,
  noticeStatusFromBody,
} from "./takeoverNotice";

const UNCLAIMED = { ack_claimed_at: null, ack_sent_at: null, ack_failed_at: null };
const CLAIMED_AT = "2026-10-02T05:00:00Z";

describe("noticeStatusFromBody", () => {
  it("reads every status services/agent/takeover_ack.py answers with", () => {
    for (const status of [
      "sent",
      "outside_window",
      "already_claimed",
      "takeover_ended",
      "not_found",
      "failed",
      "unavailable",
    ] as const) {
      expect(noticeStatusFromBody({ status })).toBe(status);
    }
  });

  it("reads anything else as unreachable", () => {
    expect(noticeStatusFromBody(null)).toBe("unreachable");
    expect(noticeStatusFromBody("sent")).toBe("unreachable");
    expect(noticeStatusFromBody({ status: "SENT" })).toBe("unreachable");
    expect(noticeStatusFromBody({ detail: "unauthorized" })).toBe("unreachable");
  });
});

describe("canRetryNotice", () => {
  it("offers another try only when nothing could be claimed", () => {
    expect(canRetryNotice("unreachable")).toBe(true);
    expect(canRetryNotice("unavailable")).toBe(true);
    expect(canRetryNotice("sent")).toBe(false);
    expect(canRetryNotice("failed")).toBe(false);
    expect(canRetryNotice("outside_window")).toBe(false);
  });
});

describe("noticeState", () => {
  it("reads the three ack columns of a takeover", () => {
    expect(noticeState(UNCLAIMED)).toBe("not_sent");
    expect(noticeState({ ...UNCLAIMED, ack_claimed_at: CLAIMED_AT })).toBe("sending");
    expect(noticeState({ ...UNCLAIMED, ack_claimed_at: CLAIMED_AT, ack_sent_at: CLAIMED_AT })).toBe(
      "sent",
    );
    expect(
      noticeState({ ...UNCLAIMED, ack_claimed_at: CLAIMED_AT, ack_failed_at: CLAIMED_AT }),
    ).toBe("failed");
  });
});

describe("labels", () => {
  it("tell staff to call the customer whenever the notice did not go out", () => {
    expect(noticeResultLabel("failed")).toContain("تواصل معه هاتفياً");
    expect(noticeResultLabel("outside_window")).toContain("تواصل معه هاتفياً");
    expect(noticeStateLabel("failed")).toContain("تواصل معه هاتفياً");
  });

  it("ask for another try when the agent could not be reached", () => {
    expect(noticeResultLabel("unreachable")).toContain("حاول مرة أخرى");
    expect(noticeResultLabel("unavailable")).toContain("حاول مرة أخرى");
  });
});
