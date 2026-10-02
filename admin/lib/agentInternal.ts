// The dashboard's one call into the agent service (owner decision D5,
// 2026-10-02): asking hotel-agent to send the takeover notice.
//
// AGENT_INTERNAL_TOKEN is read here and nowhere else in admin/. The
// "server-only" import makes the build fail if any Client Component ever
// imports this module, and the token is unprefixed (no NEXT_PUBLIC_), so
// Next.js never inlines it into a browser bundle; CI builds with a canary
// token and checks the browser output for it (scripts/check-client-bundle.mjs).
// Never log the token or the Authorization header.
import "server-only";

import { type StaffReplyStatus, replyStatusFromBody } from "@/lib/staffReply";
import { type TakeoverNoticeStatus, noticeStatusFromBody } from "@/lib/takeoverNotice";

// Where ops/hotel-agent.service binds the agent: loopback only, and nginx
// proxies nothing but /webhook/ to it, so this endpoint is never public.
export const AGENT_BASE_URL = "http://127.0.0.1:8000";

// The agent answers in well under a second; this bounds a hung call so the
// staff member's click never hangs with it (CLAUDE.md §8).
export const AGENT_REQUEST_TIMEOUT_MS = 5_000;

// A staff reply's call waits for the WhatsApp send itself, which the agent
// bounds at 10 seconds (services/agent/whatsapp_send.py), so it gets longer.
// If this runs out first the agent may still finish: the reply then shows
// as sent on the next refresh.
export const STAFF_REPLY_REQUEST_TIMEOUT_MS = 20_000;

// What a log line names the call by: the id and the key it is logged under.
interface CallLogFields {
  idKey: "takeover_id" | "staff_reply_id";
  id: number;
}

function logFailure(event: string, call: CallLogFields, detail?: string): void {
  console.error(JSON.stringify({ event, [call.idKey]: call.id, detail }));
}

/** POSTs to one of the agent's internal endpoints and returns its JSON body,
 * or null when there is no usable answer (no token configured, the agent
 * down or slow, the token refused). Never throws. */
async function postToAgent(
  path: string,
  call: CallLogFields,
  timeoutMs: number,
): Promise<unknown> {
  const token = process.env.AGENT_INTERNAL_TOKEN;
  if (!token) {
    logFailure("agent_internal_token_missing", call);
    return null;
  }
  try {
    const response = await fetch(`${AGENT_BASE_URL}${path}`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (response.status === 401) {
      logFailure("agent_internal_token_refused", call);
      return null;
    }
    return await response.json().catch(() => null);
  } catch (error) {
    logFailure(
      "agent_internal_request_failed",
      call,
      error instanceof Error ? error.name : typeof error,
    );
    return null;
  }
}

/** Asks the agent to send takeover `takeoverId`'s notice to the customer.
 * Returns the agent's status, or "unreachable" when there is no usable
 * answer (no token configured, the agent down or slow, the token refused).
 * Never throws: the takeover itself already stands either way. */
export async function requestTakeoverNotice(takeoverId: number): Promise<TakeoverNoticeStatus> {
  const body = await postToAgent(
    `/internal/takeovers/${takeoverId}/acknowledge`,
    { idKey: "takeover_id", id: takeoverId },
    AGENT_REQUEST_TIMEOUT_MS,
  );
  return noticeStatusFromBody(body);
}

/** Asks the agent to send the stored staff reply `staffReplyId` to the
 * customer. Returns the agent's status, or "unreachable" when there is no
 * usable answer. Never throws: the reply is already stored either way, and
 * the agent claims it at most once, so asking again is safe. */
export async function requestStaffReplySend(staffReplyId: number): Promise<StaffReplyStatus> {
  const body = await postToAgent(
    `/internal/staff-replies/${staffReplyId}/send`,
    { idKey: "staff_reply_id", id: staffReplyId },
    STAFF_REPLY_REQUEST_TIMEOUT_MS,
  );
  return replyStatusFromBody(body);
}
