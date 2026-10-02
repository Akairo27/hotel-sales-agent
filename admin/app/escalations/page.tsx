import Link from "next/link";
import { redirect } from "next/navigation";
import { AppShell } from "@/app/_components/AppShell";
import { PageHeader } from "@/app/_components/PageHeader";
import {
  type CustomerEscalations,
  type CustomerTakeover,
  customerPage,
  groupByCustomer,
} from "@/lib/escalationCustomers";
import {
  ESCALATION_GROUP_LABELS,
  ESCALATION_GROUPS,
  type EscalationGroup,
  formatAge,
  maskPhone,
} from "@/lib/escalations";
import { getCurrentAppUser } from "@/lib/session";
import type { EscalationRow } from "@/lib/types";
import { BADGE, BADGE_ACCENT, HINT } from "@/lib/ui";
import { createClient } from "@/utils/supabase/server";
import { LiveRefresh } from "./LiveRefresh";
import { loadActiveTakeovers } from "./_parts/takeovers";

// The escalations one list reads, newest first. Open escalations stay few
// while staff handle them; if this many are ever open at once the page says
// so, and a grouping view in the database becomes worth its migration.
const LIST_LIMIT = 1000;

type StatusFilter = "open" | "all";

function isEscalationGroup(value: string | undefined): value is EscalationGroup {
  return ESCALATION_GROUPS.some((group) => group === value);
}

function filterHref(status: StatusFilter, group: EscalationGroup | undefined): string {
  const query = new URLSearchParams({ status });
  if (group) {
    query.set("group", group);
  }
  return `/escalations?${query.toString()}`;
}

function statusLabel(customer: CustomerEscalations, currentUserId: string, now: Date): string {
  if (customer.takeover) {
    const holder =
      customer.takeover.holderId === currentUserId ? "أنت" : customer.takeover.holderName;
    return `استلمه ${holder} ${formatAge(customer.takeover.takenOverAt, now)}`;
  }
  return customer.openCount === 0 ? "مغلق" : "لم يُستلم";
}

function CustomerCard({
  customer,
  currentUserId,
  now,
}: {
  customer: CustomerEscalations;
  currentUserId: string;
  now: Date;
}) {
  const [topGroup, ...otherGroups] =
    customer.openCount > 0 ? customer.openGroups : customer.allGroups;
  return (
    <li className="min-w-0 rounded-2xl border border-border bg-surface transition hover:border-accent/50">
      <Link
        href={customerPage(customer.conversationId)}
        className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2 p-4"
      >
        <span dir="ltr" className="font-medium">
          {maskPhone(customer.customerPhone)}
        </span>
        {topGroup && (
          <span className={customer.openCount > 0 ? BADGE_ACCENT : BADGE}>
            {ESCALATION_GROUP_LABELS[topGroup]}
          </span>
        )}
        {otherGroups.map((group) => (
          <span key={group} className={BADGE}>
            {ESCALATION_GROUP_LABELS[group]}
          </span>
        ))}
        <span className={HINT}>المفتوحة: {customer.openCount}</span>
        {customer.oldestUnhandledAt && (
          <span className={HINT}>
            أقدم ما لم يُستلم: {formatAge(customer.oldestUnhandledAt, now)}
          </span>
        )}
        <span
          className={`${customer.openCount > 0 && !customer.takeover ? BADGE_ACCENT : BADGE} ms-auto`}
        >
          {statusLabel(customer, currentUserId, now)}
        </span>
      </Link>
    </li>
  );
}

// Staff notification, steps 1 and 2a (ARCHITECTURE.md §7), grouped by
// customer (owner decision 2026-10-01): one card per conversation, with who
// holds it. Customers nobody holds come first -- an open booking request
// first, then the oldest -- then the ones taken over, the current user's
// own first. Taking over happens on a customer's page. Admin and sales
// both see every escalation; migrations 0033 and 0034's policies limit the
// rows, through the user's own session.
export default async function EscalationsPage({
  searchParams,
}: {
  searchParams: Promise<{ status?: string; group?: string }>;
}) {
  const params = await searchParams;
  const appUser = await getCurrentAppUser();
  if (!appUser) {
    redirect("/login?error=" + encodeURIComponent("لا يوجد حساب مرتبط بهذا الدخول."));
  }
  const status: StatusFilter = params.status === "all" ? "all" : "open";
  const group = isEscalationGroup(params.group) ? params.group : undefined;

  const supabase = await createClient();
  let query = supabase
    .from("escalations")
    .select(
      "id, conversation_id, customer_phone, reason, notes, quote_id, opened_at, " +
        "responded_at, resolved_at, assigned_to",
    )
    .order("opened_at", { ascending: false })
    .limit(LIST_LIMIT);
  if (status === "open") {
    query = query.is("resolved_at", null);
  }
  const [{ data, error }, takeovers] = await Promise.all([
    query.overrideTypes<EscalationRow[], { merge: false }>(),
    loadActiveTakeovers(supabase),
  ]);
  const rows = data ?? [];
  const holders = new Map<number, CustomerTakeover>(
    [...takeovers.byConversation].map(([conversationId, takeover]) => [
      conversationId,
      {
        holderId: takeover.taken_over_by,
        holderName: takeover.holderName,
        takenOverAt: takeover.taken_over_at,
      },
    ]),
  );
  const customers = groupByCustomer(rows, holders, appUser.id).filter(
    (customer) =>
      !group || (status === "open" ? customer.openGroups : customer.allGroups).includes(group),
  );
  const now = new Date();

  return (
    <AppShell appUser={appUser}>
      <PageHeader
        title="التصعيدات"
        description="العملاء الذين وُعدوا بأن زميلاً سيتواصل معهم، كل عميل في سطر واحد."
      />
      <LiveRefresh channelName="escalations-list" />

      <div className="mb-4 flex min-w-0 flex-wrap items-center gap-2" role="group" aria-label="تصفية التصعيدات">
        {(["open", "all"] as const).map((option) => (
          <Link
            key={option}
            href={filterHref(option, group)}
            className={option === status ? BADGE_ACCENT : BADGE}
          >
            {option === "open" ? "المفتوحة" : "الكل"}
          </Link>
        ))}
        <Link href={filterHref(status, undefined)} className={group ? BADGE : BADGE_ACCENT}>
          كل الأسباب
        </Link>
        {ESCALATION_GROUPS.map((option) => (
          <Link
            key={option}
            href={filterHref(status, option)}
            className={option === group ? BADGE_ACCENT : BADGE}
          >
            {ESCALATION_GROUP_LABELS[option]}
          </Link>
        ))}
      </div>

      {takeovers.failed && (
        <p className={`${HINT} mb-3`}>تعذّر تحميل من استلم كل عميل. أعد تحميل الصفحة.</p>
      )}
      {rows.length === LIST_LIMIT && (
        <p className={`${HINT} mb-3`}>تظهر أحدث {LIST_LIMIT} تصعيد فقط.</p>
      )}
      {error ? (
        <p className={HINT}>تعذّر تحميل التصعيدات. أعد تحميل الصفحة.</p>
      ) : customers.length === 0 ? (
        <p className={HINT}>لا يوجد عملاء هنا.</p>
      ) : (
        <ul className="grid min-w-0 gap-3">
          {customers.map((customer) => (
            <CustomerCard
              key={customer.conversationId}
              customer={customer}
              currentUserId={appUser.id}
              now={now}
            />
          ))}
        </ul>
      )}
    </AppShell>
  );
}
