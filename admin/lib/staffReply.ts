// Staff replies from the dashboard (staff notification step 3, owner
// decisions 2026-10-02, ARCHITECTURE.md §7). Pure: the 24-hour window, what
// the agent's answer means, what a stored reply's columns mean, and the
// Arabic each is shown in. The call itself is admin/lib/agentInternal.ts,
// server-only.
import type { StaffReplyRow } from "@/lib/types";

// WhatsApp's customer service window: free text is accepted for this long
// after the customer's last message. The agent enforces it by the database
// clock (services/agent/takeover_ack.py); the dashboard only mirrors it to
// disable the box.
export const CUSTOMER_SERVICE_WINDOW_MS = 24 * 60 * 60 * 1000;

// Migration 0035's staff_replies_body_within_whatsapp_limit, in characters.
export const MAX_REPLY_LENGTH = 4096;

// A reply the agent claimed but has recorded no outcome for. The agent
// answers within seconds, so a claim this old means the outcome was lost
// (e.g. the process stopped mid-send) and the reply may or may not have
// reached the customer.
export const SENDING_STALE_MS = 2 * 60 * 1000;

/** How long free text can still be sent: the time left until the customer's
 * last message is 24 hours old, or 0 when it already is (or they never
 * wrote). The agent counts it strictly, so exactly 24 hours is closed. */
export function windowRemainingMs(lastInboundAt: string | null, now: Date): number {
  if (lastInboundAt === null) {
    return 0;
  }
  const remaining = Date.parse(lastInboundAt) + CUSTOMER_SERVICE_WINDOW_MS - now.getTime();
  return Math.max(remaining, 0);
}

/** Whether free text can still be sent (see windowRemainingMs). */
export function isWithinCustomerServiceWindow(
  lastInboundAt: string | null,
  now: Date,
): boolean {
  return windowRemainingMs(lastInboundAt, now) > 0;
}

// Below this much window left the indicator turns into a warning.
export const WINDOW_LOW_MS = 60 * 60 * 1000;

/** Why a draft cannot be queued, or null when it can. The database checks
 * the same two things; this turns them into a message before the trip. */
export function replyDraftProblem(draft: string): string | null {
  if (!/\S/.test(draft)) {
    return "اكتب نص الرد أولاً.";
  }
  if ([...draft].length > MAX_REPLY_LENGTH) {
    return `الرد أطول من ${MAX_REPLY_LENGTH} حرفاً.`;
  }
  return null;
}

// The agent's answers (services/agent/staff_reply.py), plus "unreachable"
// for no usable answer at all: the agent down, a timeout, a missing or
// rejected token, or a response this module does not recognise.
export type StaffReplyStatus =
  | "sent"
  | "outside_window"
  | "already_claimed"
  | "takeover_ended"
  | "not_found"
  | "failed"
  | "unavailable"
  | "unreachable";

const AGENT_STATUSES: readonly StaffReplyStatus[] = [
  "sent",
  "outside_window",
  "already_claimed",
  "takeover_ended",
  "not_found",
  "failed",
  "unavailable",
];

/** The status in the agent's {"status": ...} body, or "unreachable" when
 * the body is not one the agent sends. */
export function replyStatusFromBody(body: unknown): StaffReplyStatus {
  if (body === null || typeof body !== "object") {
    return "unreachable";
  }
  const status = (body as Record<string, unknown>).status;
  return AGENT_STATUSES.find((known) => known === status) ?? "unreachable";
}

// What a staff member is told right after pressing send.
const SEND_RESULT_LABELS: Record<StaffReplyStatus, string> = {
  sent: "أُرسل الرد للعميل.",
  outside_window:
    "لم يُرسل الرد: مرّ أكثر من 24 ساعة على آخر رسالة من العميل. تواصل معه هاتفياً.",
  already_claimed: "الرد أُرسل من قبل أو قيد الإرسال.",
  takeover_ended: "انتهى الاستلام قبل إرسال الرد، فلم يُرسل.",
  not_found: "لم يُعثر على هذا الرد.",
  failed: "تعذّر إرسال الرد للعميل، وقد يكون وصله. تحقق من المحادثة أو تواصل معه هاتفياً.",
  unavailable: "تعذّر إرسال الرد الآن. اضغط «إعادة المحاولة» تحت الرد.",
  // A timeout may still have been answered by the agent, so the reply's own
  // state below decides what staff do next.
  unreachable: "تعذّر تأكيد إرسال الرد. راجع حالته في الردود أدناه.",
};

export function sendResultLabel(status: StaffReplyStatus): string {
  return SEND_RESULT_LABELS[status];
}

// A reply as the database records it, on the row itself.
export type ReplyState = "sent" | "failed" | "sending" | "queued";

export function replyState(
  reply: Pick<StaffReplyRow, "claimed_at" | "sent_at" | "failed_at">,
): ReplyState {
  if (reply.sent_at !== null) {
    return "sent";
  }
  if (reply.failed_at !== null) {
    return "failed";
  }
  return reply.claimed_at !== null ? "sending" : "queued";
}

const REPLY_STATE_LABELS: Record<ReplyState, string> = {
  // Accepted by WhatsApp; a later delivery failure is not reported back.
  sent: "أُرسل",
  failed: "لم يُرسل",
  sending: "قيد الإرسال",
  queued: "لم يُرسل بعد",
};

export function replyStateLabel(state: ReplyState): string {
  return REPLY_STATE_LABELS[state];
}

const FAILURE_HINTS: Record<NonNullable<StaffReplyRow["failure_reason"]>, string> = {
  outside_window:
    "مرّ أكثر من 24 ساعة على آخر رسالة من العميل، فلا يقبل واتساب رداً حراً. تواصل معه هاتفياً.",
  send_failed:
    "تعذّر الإرسال، وقد يكون الرد وصل العميل رغم ذلك. تحقق من المحادثة أو تواصل معه هاتفياً، أو أعد كتابته.",
};

const STALE_SENDING_HINT =
  "لم يُعرف إن وصل الرد للعميل. تحقق من المحادثة أو تواصل معه هاتفياً.";

/** What to tell staff under a reply that did not go out, or null when
 * nothing is wrong (sent; still being sent; queued). A failure whose reason
 * is not one the database allows cannot occur (its CHECK), so it gets the
 * general send-failed text rather than silence. */
export function replyProblemHint(
  reply: Pick<StaffReplyRow, "claimed_at" | "sent_at" | "failed_at" | "failure_reason">,
  now: Date,
): string | null {
  const state = replyState(reply);
  if (state === "failed") {
    return FAILURE_HINTS[reply.failure_reason ?? "send_failed"];
  }
  if (
    state === "sending" &&
    reply.claimed_at !== null &&
    now.getTime() - Date.parse(reply.claimed_at) > SENDING_STALE_MS
  ) {
    return STALE_SENDING_HINT;
  }
  return null;
}

/** How a staff message is labelled in the conversation. */
export function staffMessageLabel(authorName: string | null): string {
  return authorName === null ? "الموظف" : `الموظف: ${authorName}`;
}
