// The takeover notice: the one message the agent sends a customer when a
// staff member takes their conversation over (staff notification step 2a,
// owner decision D5, ARCHITECTURE.md §7). Pure: what the agent's answer
// means, what the stored ack columns mean, and the Arabic each is shown in.
// The call itself is admin/lib/agentInternal.ts, server-only.
import type { TakeoverRow } from "@/lib/types";

// The agent's answers (services/agent/takeover_ack.py), plus "unreachable"
// for no usable answer at all: the agent down, a timeout, a missing or
// rejected token, or a response this module does not recognise.
export type TakeoverNoticeStatus =
  | "sent"
  | "outside_window"
  | "already_claimed"
  | "takeover_ended"
  | "not_found"
  | "failed"
  | "unavailable"
  | "unreachable";

const AGENT_STATUSES: readonly TakeoverNoticeStatus[] = [
  "sent",
  "outside_window",
  "already_claimed",
  "takeover_ended",
  "not_found",
  "failed",
  "unavailable",
];

/** The status in the agent's {"status": ...} body, or "unreachable" when
 * the body is not one the agent sends. The HTTP status adds nothing: the
 * agent puts the outcome in the body for every code it answers with. */
export function noticeStatusFromBody(body: unknown): TakeoverNoticeStatus {
  if (body === null || typeof body !== "object") {
    return "unreachable";
  }
  const status = (body as Record<string, unknown>).status;
  return AGENT_STATUSES.find((known) => known === status) ?? "unreachable";
}

/** Whether staff can usefully ask for the notice again: nothing was claimed
 * because the agent could not be reached or its database could not. */
export function canRetryNotice(status: TakeoverNoticeStatus): boolean {
  return status === "unreachable" || status === "unavailable";
}

// What a staff member is told right after asking for the notice.
const NOTICE_RESULT_LABELS: Record<TakeoverNoticeStatus, string> = {
  sent: "أُرسل للعميل إشعار الاستلام.",
  outside_window:
    "لم يُرسل إشعار الاستلام: مرّ أكثر من 24 ساعة على آخر رسالة من العميل. تواصل معه هاتفياً.",
  already_claimed: "إشعار الاستلام أُرسل من قبل أو قيد الإرسال.",
  takeover_ended: "انتهى الاستلام، فلم يُرسل الإشعار.",
  not_found: "لم يُعثر على هذا الاستلام.",
  failed: "تعذّر إرسال إشعار الاستلام للعميل. تواصل معه هاتفياً.",
  unavailable: "تعذّر إرسال إشعار الاستلام الآن. حاول مرة أخرى.",
  unreachable: "تعذّر الوصول لخدمة الإرسال، فلم يُرسل إشعار الاستلام. حاول مرة أخرى.",
};

export function noticeResultLabel(status: TakeoverNoticeStatus): string {
  return NOTICE_RESULT_LABELS[status];
}

// The notice as the database records it, on the takeover row itself.
export type NoticeState = "sent" | "failed" | "sending" | "not_sent";

export function noticeState(
  takeover: Pick<TakeoverRow, "ack_claimed_at" | "ack_sent_at" | "ack_failed_at">,
): NoticeState {
  if (takeover.ack_sent_at !== null) {
    return "sent";
  }
  if (takeover.ack_failed_at !== null) {
    return "failed";
  }
  return takeover.ack_claimed_at !== null ? "sending" : "not_sent";
}

const NOTICE_STATE_LABELS: Record<NoticeState, string> = {
  // Accepted by WhatsApp; a later delivery failure is not reported back.
  sent: "أُرسل للعميل إشعار الاستلام.",
  failed: "لم يُرسل للعميل إشعار الاستلام. تواصل معه هاتفياً.",
  sending: "إشعار الاستلام قيد الإرسال.",
  not_sent: "لم يُرسل إشعار الاستلام بعد.",
};

export function noticeStateLabel(state: NoticeState): string {
  return NOTICE_STATE_LABELS[state];
}
