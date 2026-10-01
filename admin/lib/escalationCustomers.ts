// The escalations screen grouped by customer -- one conversation per
// customer (owner decision 2026-10-01: a flat list of escalations does not
// scale to many customers). Pure: the pages read the rows, this module
// orders and groups them.
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

/** Open and not yet taken over by anyone (step 2 records a takeover in
 * responded_at and assigned_to). */
export function isUnhandled(escalation: EscalationRow): boolean {
  return isOpen(escalation) && escalation.responded_at === null && escalation.assigned_to === null;
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

export interface CustomerEscalations {
  conversationId: number;
  customerPhone: string;
  /** In orderEscalations' order. */
  escalations: EscalationRow[];
  openCount: number;
  /** The reasons of the open escalations, most important first. */
  openGroups: EscalationGroup[];
  /** The reasons of every escalation, open or closed, most important first. */
  allGroups: EscalationGroup[];
  /** When the oldest escalation nobody has taken over opened, or null. */
  oldestUnhandledAt: string | null;
  latestOpenedAt: string;
}

function summarize(escalations: EscalationRow[]): CustomerEscalations {
  const ordered = orderEscalations(escalations);
  const open = ordered.filter(isOpen);
  const unhandledTimes = ordered.filter(isUnhandled).map((escalation) => escalation.opened_at);
  const byTime = (a: string, b: string) => instant(a) - instant(b);
  return {
    conversationId: ordered[0].conversation_id,
    customerPhone: ordered[0].customer_phone,
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

function compareCustomers(a: CustomerEscalations, b: CustomerEscalations): number {
  if ((a.openCount > 0) !== (b.openCount > 0)) {
    return a.openCount > 0 ? -1 : 1;
  }
  if (a.openCount === 0) {
    return instant(b.latestOpenedAt) - instant(a.latestOpenedAt) || a.conversationId - b.conversationId;
  }
  if (hasOpenBooking(a) !== hasOpenBooking(b)) {
    return hasOpenBooking(a) ? -1 : 1;
  }
  if ((a.oldestUnhandledAt === null) !== (b.oldestUnhandledAt === null)) {
    return a.oldestUnhandledAt === null ? 1 : -1;
  }
  const byUnhandled =
    a.oldestUnhandledAt !== null && b.oldestUnhandledAt !== null
      ? instant(a.oldestUnhandledAt) - instant(b.oldestUnhandledAt)
      : 0;
  return byUnhandled || a.conversationId - b.conversationId;
}

/** One entry per customer (conversation), sorted the way the owner asked:
 * customers with an open booking request first, then the oldest unhandled
 * escalation first; customers with nothing open come last, newest first. */
export function groupByCustomer(escalations: readonly EscalationRow[]): CustomerEscalations[] {
  const byConversation = new Map<number, EscalationRow[]>();
  for (const escalation of escalations) {
    const rows = byConversation.get(escalation.conversation_id) ?? [];
    rows.push(escalation);
    byConversation.set(escalation.conversation_id, rows);
  }
  return [...byConversation.values()].map(summarize).sort(compareCustomers);
}

/** A customer's page on the escalations screen. */
export function customerPage(conversationId: number): string {
  return `/escalations/conversations/${conversationId}`;
}

/** Where one escalation is shown: its customer's page, at that escalation. */
export function customerPageFor(conversationId: number, escalationId: number): string {
  return `${customerPage(conversationId)}#escalation-${escalationId}`;
}
