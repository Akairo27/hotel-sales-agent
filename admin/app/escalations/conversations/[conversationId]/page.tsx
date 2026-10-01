import { notFound, redirect } from "next/navigation";
import { AppShell } from "@/app/_components/AppShell";
import { PageHeader } from "@/app/_components/PageHeader";
import { isOpen, orderEscalations } from "@/lib/escalationCustomers";
import {
  escalationLabel,
  formatAge,
  formatRiyadhDateTime,
  notOpenStays,
  parseNotes,
  technicalNotes,
} from "@/lib/escalations";
import { formatHalalasAsRiyal } from "@/lib/money";
import { getCurrentAppUser } from "@/lib/session";
import type {
  ConversationRow,
  EscalationRow,
  MessageRow,
  QuoteSummaryRow,
} from "@/lib/types";
import { BADGE, BADGE_ACCENT, CARD, HINT, SECTION_TITLE } from "@/lib/ui";
import { createClient } from "@/utils/supabase/server";
import { LiveRefresh } from "../../LiveRefresh";
import { ReasonDetails } from "../../_parts/ReasonDetails";
import { loadStayNames, type StayNames } from "../../_parts/names";

// The latest messages of the conversation shown, oldest first.
const MESSAGE_LIMIT = 100;

const ESCALATION_COLUMNS =
  "id, conversation_id, customer_phone, reason, notes, quote_id, opened_at, " +
  "responded_at, resolved_at, assigned_to";
const QUOTE_COLUMNS =
  "id, hotel_id, room_type_id, check_in, check_out, rooms, ask_price_total, created_at";

function EscalationEntry({
  escalation,
  quotes,
  names,
  now,
}: {
  escalation: EscalationRow;
  quotes: QuoteSummaryRow[];
  names: StayNames;
  now: Date;
}) {
  const notes = parseNotes(escalation.notes);
  const technical = technicalNotes(notes);
  const open = isOpen(escalation);
  return (
    <details
      id={`escalation-${escalation.id}`}
      open={open}
      className="rounded-xl border border-border p-4 target:border-accent"
    >
      <summary className="flex cursor-pointer flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-medium">رقم {escalation.id}</span>
        <span className={open ? BADGE_ACCENT : BADGE}>{escalationLabel(escalation.reason)}</span>
        <span className={HINT}>
          <span dir="ltr">{formatRiyadhDateTime(escalation.opened_at)}</span> ·{" "}
          {formatAge(escalation.opened_at, now)}
        </span>
        <span className={HINT}>{open ? "مفتوح" : "مغلق"}</span>
      </summary>
      <div className="mt-3 min-w-0 break-words">
        <ReasonDetails
          escalation={escalation}
          notes={notes}
          quote={quotes.find((quote) => quote.id === escalation.quote_id)}
          names={names}
        />
        {technical.length > 0 && (
          <details className="mt-3">
            <summary className={HINT}>تفاصيل تقنية</summary>
            <dl className="mt-2 grid gap-1 text-sm" dir="ltr">
              {technical.map(([key, value]) => (
                <div key={key} className="min-w-0">
                  <dt className="inline font-medium">{key}: </dt>
                  <dd className="inline break-all">{value}</dd>
                </div>
              ))}
            </dl>
          </details>
        )}
      </div>
    </details>
  );
}

/** For each message, the escalations that opened after the previous message
 * and no later than this one -- marked just before it. Escalations newer
 * than the last message are marked after the conversation. */
function escalationsBeforeEachMessage(
  messages: MessageRow[],
  escalations: EscalationRow[],
): { before: Map<number, EscalationRow[]>; after: EscalationRow[] } {
  const chronological = [...escalations].sort(
    (a, b) => Date.parse(a.opened_at) - Date.parse(b.opened_at),
  );
  const before = new Map<number, EscalationRow[]>();
  let next = 0;
  for (const message of messages) {
    const until = Date.parse(message.created_at);
    const marked: EscalationRow[] = [];
    while (next < chronological.length && Date.parse(chronological[next].opened_at) <= until) {
      marked.push(chronological[next]);
      next += 1;
    }
    before.set(message.id, marked);
  }
  return { before, after: chronological.slice(next) };
}

function EscalationMarker({ escalation }: { escalation: EscalationRow }) {
  return (
    <p className="my-2 text-center text-xs text-muted-foreground">
      — تصعيد رقم {escalation.id}: {escalationLabel(escalation.reason)} —
    </p>
  );
}

// One customer's escalations and conversation, read-only (staff
// notification step 1, ARCHITECTURE.md §7; grouped by customer, owner
// decision 2026-10-01). Every read goes through the signed-in user's own
// session, so migration 0033's policies decide what exists here; customer
// text is rendered as text, never as HTML.
export default async function CustomerEscalationsPage({
  params,
}: {
  params: Promise<{ conversationId: string }>;
}) {
  const { conversationId: rawId } = await params;
  const appUser = await getCurrentAppUser();
  if (!appUser) {
    redirect("/login?error=" + encodeURIComponent("لا يوجد حساب مرتبط بهذا الدخول."));
  }
  const conversationId = Number(rawId);
  if (!Number.isSafeInteger(conversationId) || conversationId <= 0) {
    notFound();
  }

  const supabase = await createClient();
  const [conversationResult, escalationsResult, messagesResult, quotesResult] = await Promise.all([
    supabase
      .from("conversations")
      .select("id, customer_phone, last_message_at")
      .eq("id", conversationId)
      .maybeSingle<ConversationRow>(),
    supabase
      .from("escalations")
      .select(ESCALATION_COLUMNS)
      .eq("conversation_id", conversationId)
      .overrideTypes<EscalationRow[], { merge: false }>(),
    supabase
      .from("messages")
      .select("id, direction, body, created_at")
      .eq("conversation_id", conversationId)
      .order("created_at", { ascending: false })
      .order("id", { ascending: false })
      .limit(MESSAGE_LIMIT)
      .overrideTypes<MessageRow[], { merge: false }>(),
    supabase
      .from("quotes")
      .select(QUOTE_COLUMNS)
      .eq("conversation_id", conversationId)
      .order("created_at", { ascending: false })
      .overrideTypes<QuoteSummaryRow[], { merge: false }>(),
  ]);
  const conversation = conversationResult.data;
  const escalations = orderEscalations(escalationsResult.data ?? []);
  if (!conversation || escalations.length === 0) {
    notFound();
  }
  const messages = [...(messagesResult.data ?? [])].reverse();
  const quotes = quotesResult.data ?? [];
  const stays = escalations.flatMap((escalation) => notOpenStays(parseNotes(escalation.notes)));
  const names = await loadStayNames(
    supabase,
    [...quotes.map((quote) => quote.hotel_id), ...stays.map((stay) => stay.hotelId)],
    [...quotes.map((quote) => quote.room_type_id), ...stays.map((stay) => stay.roomTypeId)],
  );
  const { before, after } = escalationsBeforeEachMessage(messages, escalations);
  const openCount = escalations.filter(isOpen).length;
  const now = new Date();

  return (
    <AppShell appUser={appUser}>
      <PageHeader
        breadcrumb={{ href: "/escalations", label: "التصعيدات" }}
        title="تصعيدات العميل"
        description={`المفتوحة: ${openCount} من ${escalations.length}`}
      />
      <LiveRefresh channelName={`escalations-customer-${conversationId}`} conversationId={conversationId} />

      <div className="grid min-w-0 gap-6">
        <section className={CARD}>
          <h2 className={SECTION_TITLE}>العميل</h2>
          <p className="mt-2">
            <a href={`tel:${conversation.customer_phone}`} dir="ltr" className="font-medium underline">
              {conversation.customer_phone}
            </a>
          </p>
          <p className={HINT}>
            {openCount > 0 ? "لم يستلم أحد هذه التصعيدات بعد." : "كل تصعيدات هذا العميل مغلقة."}
          </p>
        </section>

        <section className={CARD}>
          <h2 className={SECTION_TITLE}>التصعيدات</h2>
          <div className="mt-3 grid gap-3">
            {escalations.map((escalation) => (
              <EscalationEntry
                key={escalation.id}
                escalation={escalation}
                quotes={quotes}
                names={names}
                now={now}
              />
            ))}
          </div>
        </section>

        <section className={CARD}>
          <h2 className={SECTION_TITLE}>المحادثة</h2>
          {messages.length === MESSAGE_LIMIT && (
            <p className={HINT}>تظهر آخر {MESSAGE_LIMIT} رسالة فقط.</p>
          )}
          <ol className="mt-3 space-y-2">
            {messages.map((message) => (
              <li key={message.id} className="min-w-0">
                {(before.get(message.id) ?? []).map((escalation) => (
                  <EscalationMarker key={escalation.id} escalation={escalation} />
                ))}
                <div
                  className={
                    message.direction === "inbound"
                      ? "me-12 rounded-xl border border-border bg-surface-subtle p-3"
                      : "ms-12 rounded-xl border border-border p-3"
                  }
                >
                  <p className="text-xs text-muted-foreground">
                    {message.direction === "inbound" ? "العميل" : "الوكيل"} ·{" "}
                    <span dir="ltr">{formatRiyadhDateTime(message.created_at)}</span>
                  </p>
                  <p className="mt-1 whitespace-pre-wrap break-words">{message.body}</p>
                </div>
              </li>
            ))}
          </ol>
          {after.map((escalation) => (
            <EscalationMarker key={escalation.id} escalation={escalation} />
          ))}
        </section>

        {quotes.length > 0 && (
          <section className={CARD}>
            <h2 className={SECTION_TITLE}>العروض في هذه المحادثة</h2>
            <ul className="mt-3 grid gap-2">
              {quotes.map((quote) => (
                <li key={quote.id} className="rounded-xl border border-border p-3">
                  <p className="font-medium">
                    {names.hotels.get(quote.hotel_id) ?? `الفندق ${quote.hotel_id}`}،{" "}
                    {names.roomTypes.get(quote.room_type_id) ?? `نوع الغرفة ${quote.room_type_id}`}
                  </p>
                  <p className={HINT}>
                    العرض رقم {quote.id} · <span dir="ltr">{quote.check_in} → {quote.check_out}</span> ·
                    عدد الغرف {quote.rooms} · الإجمالي {formatHalalasAsRiyal(quote.ask_price_total)}
                  </p>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </AppShell>
  );
}
