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

import { type TakeoverNoticeStatus, noticeStatusFromBody } from "@/lib/takeoverNotice";

// Where ops/hotel-agent.service binds the agent: loopback only, and nginx
// proxies nothing but /webhook/ to it, so this endpoint is never public.
export const AGENT_BASE_URL = "http://127.0.0.1:8000";

// The agent answers in well under a second; this bounds a hung call so the
// staff member's click never hangs with it (CLAUDE.md §8).
export const AGENT_REQUEST_TIMEOUT_MS = 5_000;

function logFailure(event: string, takeoverId: number, detail?: string): void {
  console.error(JSON.stringify({ event, takeover_id: takeoverId, detail }));
}

/** Asks the agent to send takeover `takeoverId`'s notice to the customer.
 * Returns the agent's status, or "unreachable" when there is no usable
 * answer (no token configured, the agent down or slow, the token refused).
 * Never throws: the takeover itself already stands either way. */
export async function requestTakeoverNotice(takeoverId: number): Promise<TakeoverNoticeStatus> {
  const token = process.env.AGENT_INTERNAL_TOKEN;
  if (!token) {
    logFailure("agent_internal_token_missing", takeoverId);
    return "unreachable";
  }
  try {
    const response = await fetch(
      `${AGENT_BASE_URL}/internal/takeovers/${takeoverId}/acknowledge`,
      {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        cache: "no-store",
        signal: AbortSignal.timeout(AGENT_REQUEST_TIMEOUT_MS),
      },
    );
    if (response.status === 401) {
      logFailure("agent_internal_token_refused", takeoverId);
      return "unreachable";
    }
    const body: unknown = await response.json().catch(() => null);
    return noticeStatusFromBody(body);
  } catch (error) {
    logFailure(
      "agent_internal_request_failed",
      takeoverId,
      error instanceof Error ? error.name : typeof error,
    );
    return "unreachable";
  }
}
