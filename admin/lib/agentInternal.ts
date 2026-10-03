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

// A call with no id (the template status read) logs with none.
function logFailure(event: string, call: CallLogFields | null, detail?: string): void {
  console.error(JSON.stringify({ event, ...(call ? { [call.idKey]: call.id } : {}), detail }));
}

/** Calls one of the agent's internal endpoints and returns its JSON body,
 * or null when there is no usable answer (no token configured, the agent
 * down or slow, the token refused). Never throws. */
async function callAgent(
  method: "GET" | "POST",
  path: string,
  call: CallLogFields | null,
  timeoutMs: number,
): Promise<unknown> {
  const token = process.env.AGENT_INTERNAL_TOKEN;
  if (!token) {
    logFailure("agent_internal_token_missing", call);
    return null;
  }
  try {
    const response = await fetch(`${AGENT_BASE_URL}${path}`, {
      method,
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
  const body = await callAgent(
    "POST",
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
  const body = await callAgent(
    "POST",
    `/internal/staff-replies/${staffReplyId}/send`,
    { idKey: "staff_reply_id", id: staffReplyId },
    STAFF_REPLY_REQUEST_TIMEOUT_MS,
  );
  return replyStatusFromBody(body);
}

/** Whether the agent can send the re-engagement template: both template
 * names are set in agent.env (PR C). False when the agent says so and when
 * there is no usable answer, so the dashboard's template button stays off
 * rather than offering something that cannot be sent. Never throws. */
export async function requestReengagementTemplateEnabled(): Promise<boolean> {
  const body = await callAgent(
    "GET",
    "/internal/reengagement-template",
    null,
    AGENT_REQUEST_TIMEOUT_MS,
  );
  return (
    body !== null &&
    typeof body === "object" &&
    (body as Record<string, unknown>).enabled === true
  );
}
