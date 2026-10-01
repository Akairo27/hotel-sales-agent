import Link from "next/link";
import { redirect } from "next/navigation";
import { AppShell } from "@/app/_components/AppShell";
import { PageHeader } from "@/app/_components/PageHeader";
import {
  ESCALATION_GROUP_LABELS,
  ESCALATION_GROUPS,
  type EscalationGroup,
  escalationGroup,
  escalationLabel,
  formatAge,
  formatRiyadhDateTime,
  maskPhone,
} from "@/lib/escalations";
import { getCurrentAppUser } from "@/lib/session";
import type { EscalationRow } from "@/lib/types";
import { BADGE, BADGE_ACCENT, HINT, TABLE, TABLE_ROW, TABLE_WRAPPER, TD, TH } from "@/lib/ui";
import { createClient } from "@/utils/supabase/server";
import { LiveRefresh } from "./LiveRefresh";

// The newest escalations a list shows. Older ones stay readable from
// their own page and from their conversation's other escalations.
const LIST_LIMIT = 200;

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

function StatusBadge({ escalation }: { escalation: EscalationRow }) {
  if (escalation.resolved_at) {
    return <span className={BADGE}>مغلق</span>;
  }
  return <span className={BADGE_ACCENT}>لم يُستلم</span>;
}

// Staff notification, step 1 (ARCHITECTURE.md §7): read-only. Admin and
// sales both see every escalation (owner decision 2026-10-01); migration
// 0033's policies are what limit the rows, through the user's own session.
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
  const { data, error } = await query.overrideTypes<EscalationRow[], { merge: false }>();
  const escalations = (data ?? []).filter(
    (escalation) => !group || escalationGroup(escalation.reason) === group,
  );
  const now = new Date();

  return (
    <AppShell appUser={appUser}>
      <PageHeader
        title="التصعيدات"
        description="طلبات العملاء التي وُعد فيها العميل بأن زميلاً سيتواصل معه."
      />
      <LiveRefresh channelName="escalations-list" />

      <nav className="mb-4 flex flex-wrap gap-2" aria-label="تصفية التصعيدات">
        {(["open", "all"] as const).map((option) => (
          <Link
            key={option}
            href={filterHref(option, group)}
            className={option === status ? BADGE_ACCENT : BADGE}
          >
            {option === "open" ? "المفتوحة" : "الكل"}
          </Link>
        ))}
        <span className="mx-2 border-s border-border" aria-hidden />
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
      </nav>

      {error ? (
        <p className={HINT}>تعذّر تحميل التصعيدات. أعد تحميل الصفحة.</p>
      ) : escalations.length === 0 ? (
        <p className={HINT}>لا توجد تصعيدات هنا.</p>
      ) : (
        <div className={TABLE_WRAPPER}>
          <table className={TABLE}>
            <thead>
              <tr>
                <th className={TH}>الرقم</th>
                <th className={TH}>السبب</th>
                <th className={TH}>وقت الفتح (الرياض)</th>
                <th className={TH}>العميل</th>
                <th className={TH}>الحالة</th>
              </tr>
            </thead>
            <tbody>
              {escalations.map((escalation) => (
                <tr key={escalation.id} className={TABLE_ROW}>
                  <td className={TD}>
                    <Link href={`/escalations/${escalation.id}`} className="font-medium underline">
                      {escalation.id}
                    </Link>
                  </td>
                  <td className={TD}>{escalationLabel(escalation.reason)}</td>
                  <td className={TD}>
                    <span dir="ltr">{formatRiyadhDateTime(escalation.opened_at)}</span>
                    <span className="ms-2 text-muted-foreground">
                      {formatAge(escalation.opened_at, now)}
                    </span>
                  </td>
                  <td className={TD}>
                    <span dir="ltr">{maskPhone(escalation.customer_phone)}</span>
                  </td>
                  <td className={TD}>
                    <StatusBadge escalation={escalation} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </AppShell>
  );
}
