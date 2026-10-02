// The escalations screen grouped by customer -- one conversation per
// customer (owner decision 2026-10-01: a flat list of escalations does not
// scale to many customers). Pure: the pages read the rows, this module
// orders and groups them. Who holds a customer comes from the active
// takeover of the conversation (migration 0034), never from
// escalations.assigned_to/responded_at, which stay unused.
import { type EscalationGroup, escalationGroup } from "@/lib/escalations";
import type { EscalationRow } from "@/lib/types";

// The order a customer's open reasons are ranked in (owner-approved
// 2026-10-01): the first is the one the list shows, and a customer with an
// open booking request sorts before every other.
export const REASON_PRIORITY: readonly EscalationGroup[] = [
  "booking",
  "dates_not_open",
  "guard",
  "failure",
  "unanswered",
  "caps",
  "media",
];

function rank(escalation: EscalationRow): number {
  return REASON_PRIORITY.indexOf(escalationGroup(escalation.reason));
}

function instant(timestamp: string): number {
  return Date.parse(timestamp);
}

export function isOpen(escalation: EscalationRow): boolean {
  return escalation.resolved_at === null;
}

/** Open escalations first -- the most important reason first, then the
 * oldest -- then closed ones, newest first. */
export function orderEscalations(escalations: readonly EscalationRow[]): EscalationRow[] {
  return [...escalations].sort((a, b) => {
    if (isOpen(a) !== isOpen(b)) {
      return isOpen(a) ? -1 : 1;
    }
    if (isOpen(a)) {
      return rank(a) - rank(b) || instant(a.opened_at) - instant(b.opened_at) || a.id - b.id;
    }
    return instant(b.opened_at) - instant(a.opened_at) || b.id - a.id;
  });
}

function distinctGroups(escalations: readonly EscalationRow[]): EscalationGroup[] {
  const groups = new Set(escalations.map((escalation) => escalationGroup(escalation.reason)));
  return REASON_PRIORITY.filter((group) => groups.has(group));
}

/** The staff member holding a customer's conversation: its active
 * takeover. */
export interface CustomerTakeover {
  holderId: string;
  holderName: string;
  takenOverAt: string;
}

export interface CustomerEscalations {
  conversationId: number;
  customerPhone: string;
  /** Who holds the conversation now, or null when nobody does. */
  takeover: CustomerTakeover | null;
  /** In orderEscalations' order. */
  escalations: EscalationRow[];
  openCount: number;
  /** The reasons of the open escalations, most important first. */
  openGroups: EscalationGroup[];
  /** The reasons of every escalation, open or closed, most important first. */
  allGroups: EscalationGroup[];
  /** When the oldest open escalation opened, while nobody holds the
   * conversation; null once someone does, or when nothing is open. */
  oldestUnhandledAt: string | null;
  latestOpenedAt: string;
}

function summarize(
  escalations: EscalationRow[],
  takeover: CustomerTakeover | null,
): CustomerEscalations {
  const ordered = orderEscalations(escalations);
  const open = ordered.filter(isOpen);
  const unhandledTimes = takeover ? [] : open.map((escalation) => escalation.opened_at);
  const byTime = (a: string, b: string) => instant(a) - instant(b);
  return {
    conversationId: ordered[0].conversation_id,
    customerPhone: ordered[0].customer_phone,
    takeover,
    escalations: ordered,
    openCount: open.length,
    openGroups: distinctGroups(open),
    allGroups: distinctGroups(ordered),
    oldestUnhandledAt: unhandledTimes.sort(byTime)[0] ?? null,
    latestOpenedAt: ordered.map((escalation) => escalation.opened_at).sort(byTime).at(-1) ?? "",
  };
}

function hasOpenBooking(customer: CustomerEscalations): boolean {
  return customer.openGroups[0] === "booking";
}

function compareUnclaimed(a: CustomerEscalations, b: CustomerEscalations): number {
  if (hasOpenBooking(a) !== hasOpenBooking(b)) {
    return hasOpenBooking(a) ? -1 : 1;
  }
  const byUnhandled =
    a.oldestUnhandledAt !== null && b.oldestUnhandledAt !== null
      ? instant(a.oldestUnhandledAt) - instant(b.oldestUnhandledAt)
      : 0;
  return byUnhandled || a.conversationId - b.conversationId;
}

function compareClaimed(
  a: CustomerTakeover,
  b: CustomerTakeover,
  currentUserId: string,
): number {
  const aMine = a.holderId === currentUserId;
  const bMine = b.holderId === currentUserId;
  if (aMine !== bMine) {
    return aMine ? -1 : 1;
  }
  return instant(a.takenOverAt) - instant(b.takenOverAt);
}

function compareCustomers(
  a: CustomerEscalations,
  b: CustomerEscalations,
  currentUserId: string,
): number {
  if ((a.openCount > 0) !== (b.openCount > 0)) {
    return a.openCount > 0 ? -1 : 1;
  }
  if (a.openCount === 0) {
    return instant(b.latestOpenedAt) - instant(a.latestOpenedAt) || a.conversationId - b.conversationId;
  }
  if ((a.takeover === null) !== (b.takeover === null)) {
    return a.takeover === null ? -1 : 1;
  }
  if (a.takeover !== null && b.takeover !== null) {
    return compareClaimed(a.takeover, b.takeover, currentUserId) || a.conversationId - b.conversationId;
  }
  return compareUnclaimed(a, b);
}

/** One entry per customer (conversation), sorted the way the owner asked:
 * customers nobody holds first -- those with an open booking request
 * first, then the oldest open escalation first -- then the ones a staff
 * member holds, the current user's own first, then the longest held;
 * customers with nothing open come last, newest first. `takeovers` maps a
 * conversation to its active takeover. */
export function groupByCustomer(
  escalations: readonly EscalationRow[],
  takeovers: ReadonlyMap<number, CustomerTakeover>,
  currentUserId: string,
): CustomerEscalations[] {
  const byConversation = new Map<number, EscalationRow[]>();
  for (const escalation of escalations) {
    const rows = byConversation.get(escalation.conversation_id) ?? [];
    rows.push(escalation);
    byConversation.set(escalation.conversation_id, rows);
  }
  return [...byConversation.entries()]
    .map(([conversationId, rows]) => summarize(rows, takeovers.get(conversationId) ?? null))
    .sort((a, b) => compareCustomers(a, b, currentUserId));
}

/** A customer's page on the escalations screen. */
export function customerPage(conversationId: number): string {
  return `/escalations/conversations/${conversationId}`;
}

/** Where one escalation is shown: its customer's page, at that escalation. */
export function customerPageFor(conversationId: number, escalationId: number): string {
  return `${customerPage(conversationId)}#escalation-${escalationId}`;
}
