import Link from "next/link";
import { notFound, redirect } from "next/navigation";
import { AppShell } from "@/app/_components/AppShell";
import { PageHeader } from "@/app/_components/PageHeader";
import {
  escalationGroup,
  escalationLabel,
  formatAge,
  formatRiyadhDateTime,
  halalasList,
  notOpenStays,
  parseNotes,
  stringList,
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
import { CARD, HINT, SECTION_TITLE, TABLE, TABLE_WRAPPER, TD, TH } from "@/lib/ui";
import { createClient } from "@/utils/supabase/server";
import { LiveRefresh } from "../LiveRefresh";

// The latest messages of the conversation shown, oldest first.
const MESSAGE_LIMIT = 100;

const ESCALATION_COLUMNS =
  "id, conversation_id, customer_phone, reason, notes, quote_id, opened_at, " +
  "responded_at, resolved_at, assigned_to";
const QUOTE_COLUMNS =
  "id, hotel_id, room_type_id, check_in, check_out, rooms, ask_price_total, created_at";

type Names = Map<number, string>;

async function loadNames(
  supabase: Awaited<ReturnType<typeof createClient>>,
  hotelIds: number[],
  roomTypeIds: number[],
): Promise<{ hotels: Names; roomTypes: Names }> {
  const [{ data: hotels }, { data: roomTypes }] = await Promise.all([
    supabase.from("hotels").select("id, hotel_name").in("id", hotelIds),
    supabase.from("room_types").select("id, room_type_name").in("id", roomTypeIds),
  ]);
  return {
    hotels: new Map((hotels ?? []).map((hotel) => [hotel.id as number, hotel.hotel_name as string])),
    roomTypes: new Map(
      (roomTypes ?? []).map((roomType) => [roomType.id as number, roomType.room_type_name as string]),
    ),
  };
}

function ReasonDetails({
  escalation,
  notes,
  quote,
  names,
}: {
  escalation: EscalationRow;
  notes: Record<string, unknown>;
  quote: QuoteSummaryRow | undefined;
  names: { hotels: Names; roomTypes: Names };
}) {
  const group = escalationGroup(escalation.reason);
  if (group === "booking" && quote) {
    return (
      <p>
        طلب العميل حجز {names.hotels.get(quote.hotel_id) ?? `الفندق ${quote.hotel_id}`}،{" "}
        {names.roomTypes.get(quote.room_type_id) ?? `نوع الغرفة ${quote.room_type_id}`}، من{" "}
        <span dir="ltr">{quote.check_in}</span> إلى <span dir="ltr">{quote.check_out}</span>، عدد
        الغرف {quote.rooms}، الإجمالي {formatHalalasAsRiyal(quote.ask_price_total)} (العرض رقم{" "}
        {quote.id}).
      </p>
    );
  }
  if (group === "dates_not_open") {
    return (
      <ul className="list-disc ps-5">
        {notOpenStays(notes).map((stay) => (
          <li key={`${stay.hotelId}-${stay.roomTypeId}`}>
            {names.hotels.get(stay.hotelId) ?? `الفندق ${stay.hotelId}`}،{" "}
            {names.roomTypes.get(stay.roomTypeId) ?? `نوع الغرفة ${stay.roomTypeId}`}: ليالٍ لم
            يُفتح حجزها <span dir="ltr">{stay.nights.join(", ")}</span>
          </li>
        ))}
      </ul>
    );
  }
  if (group === "guard") {
    const claims = stringList(notes.booking_claims);
    const amounts = halalasList(notes.blocked_amounts_halalas);
    return (
      <div className="space-y-2">
        <p>حجب حارس الرسائل هذا الرد ولم يصل العميل، ووصلته الرسالة الاحتياطية بدلاً منه:</p>
        {typeof notes.blocked_reply_text === "string" && (
          <blockquote className="whitespace-pre-wrap rounded-xl border border-border bg-surface-subtle p-3">
            {notes.blocked_reply_text}
          </blockquote>
        )}
        {claims.length > 0 && <p>عبارات ادعاء حجز: {claims.join("، ")}</p>}
        {amounts.length > 0 && (
          <p>مبالغ محجوبة: {amounts.map((amount) => formatHalalasAsRiyal(amount)).join("، ")}</p>
        )}
      </div>
    );
  }
  if (group === "media" && typeof notes.message_type === "string") {
    return <p>نوع الرسالة: {notes.message_type}</p>;
  }
  return <p className={HINT}>وصلت العميلَ الرسالة الاحتياطية بأن زميلاً سيتواصل معه.</p>;
}

// One escalation, read-only (staff notification step 1, ARCHITECTURE.md
// §7). Every read goes through the signed-in user's session, so migration
// 0033's policies decide what exists here; customer text is rendered as
// text, never as HTML.
export default async function EscalationPage({
  params,
}: {
  params: Promise<{ escalationId: string }>;
}) {
  const { escalationId } = await params;
  const appUser = await getCurrentAppUser();
  if (!appUser) {
    redirect("/login?error=" + encodeURIComponent("لا يوجد حساب مرتبط بهذا الدخول."));
  }
  const id = Number(escalationId);
  if (!Number.isSafeInteger(id) || id <= 0) {
    notFound();
  }

  const supabase = await createClient();
  const { data: escalation } = await supabase
    .from("escalations")
    .select(ESCALATION_COLUMNS)
    .eq("id", id)
    .maybeSingle<EscalationRow>();
  if (!escalation) {
    notFound();
  }

  const conversationId = escalation.conversation_id;
  const [conversationResult, messagesResult, quotesResult, othersResult] = await Promise.all([
    supabase
      .from("conversations")
      .select("id, customer_phone, last_message_at")
      .eq("id", conversationId)
      .maybeSingle<ConversationRow>(),
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
    supabase
      .from("escalations")
      .select(ESCALATION_COLUMNS)
      .eq("conversation_id", conversationId)
      .neq("id", id)
      .order("opened_at", { ascending: false })
      .overrideTypes<EscalationRow[], { merge: false }>(),
  ]);
  const messages = [...(messagesResult.data ?? [])].reverse();
  const quotes = quotesResult.data ?? [];
  const others = othersResult.data ?? [];
  const notes = parseNotes(escalation.notes);
  const stays = notOpenStays(notes);
  const names = await loadNames(
    supabase,
    [...quotes.map((quote) => quote.hotel_id), ...stays.map((stay) => stay.hotelId)],
    [...quotes.map((quote) => quote.room_type_id), ...stays.map((stay) => stay.roomTypeId)],
  );
  const phone = conversationResult.data?.customer_phone ?? escalation.customer_phone;
  const technical = technicalNotes(notes);
  // Compared as instants: Postgres trims trailing zeros from fractional
  // seconds, so the ISO strings do not sort reliably as text.
  const openedAt = new Date(escalation.opened_at).getTime();
  const firstAfterEscalation = messages.findIndex(
    (message) => new Date(message.created_at).getTime() >= openedAt,
  );
  const now = new Date();

  return (
    <AppShell appUser={appUser}>
      <PageHeader
        breadcrumb={{ href: "/escalations", label: "التصعيدات" }}
        title={`تصعيد رقم ${escalation.id}: ${escalationLabel(escalation.reason)}`}
        description={`فُتح ${formatAge(escalation.opened_at, now)} — ${formatRiyadhDateTime(escalation.opened_at)} بتوقيت الرياض`}
      />
      <LiveRefresh channelName={`escalation-${escalation.id}`} conversationId={conversationId} />

      <div className="grid gap-6">
        <section className={CARD}>
          <h2 className={SECTION_TITLE}>العميل</h2>
          <p className="mt-2">
            <a href={`tel:${phone}`} dir="ltr" className="font-medium underline">
              {phone}
            </a>
          </p>
          <p className={HINT}>
            {escalation.resolved_at
              ? `أُغلق ${formatRiyadhDateTime(escalation.resolved_at)}`
              : "لم يستلمه أحد بعد."}
          </p>
        </section>

        <section className={CARD}>
          <h2 className={SECTION_TITLE}>السبب</h2>
          <div className="mt-2">
            <ReasonDetails
              escalation={escalation}
              notes={notes}
              quote={quotes.find((quote) => quote.id === escalation.quote_id)}
              names={names}
            />
          </div>
          {technical.length > 0 && (
            <details className="mt-3">
              <summary className={HINT}>تفاصيل تقنية</summary>
              <dl className="mt-2 grid gap-1 text-sm" dir="ltr">
                {technical.map(([key, value]) => (
                  <div key={key}>
                    <dt className="inline font-medium">{key}: </dt>
                    <dd className="inline break-all">{value}</dd>
                  </div>
                ))}
              </dl>
            </details>
          )}
        </section>

        <section className={CARD}>
          <h2 className={SECTION_TITLE}>المحادثة</h2>
          {messages.length === MESSAGE_LIMIT && (
            <p className={HINT}>تظهر آخر {MESSAGE_LIMIT} رسالة فقط.</p>
          )}
          <ol className="mt-3 space-y-2">
            {messages.map((message, index) => (
              <li key={message.id}>
                {index === firstAfterEscalation && (
                  <p className="my-2 text-center text-xs text-muted-foreground">— وقت التصعيد —</p>
                )}
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
                  <p className="mt-1 whitespace-pre-wrap">{message.body}</p>
                </div>
              </li>
            ))}
          </ol>
        </section>

        {quotes.length > 0 && (
          <section>
            <h2 className={SECTION_TITLE}>العروض في هذه المحادثة</h2>
            <div className={`${TABLE_WRAPPER} mt-3`}>
              <table className={TABLE}>
                <thead>
                  <tr>
                    <th className={TH}>العرض</th>
                    <th className={TH}>الفندق</th>
                    <th className={TH}>نوع الغرفة</th>
                    <th className={TH}>التواريخ</th>
                    <th className={TH}>الغرف</th>
                    <th className={TH}>الإجمالي</th>
                  </tr>
                </thead>
                <tbody>
                  {quotes.map((quote) => (
                    <tr key={quote.id}>
                      <td className={TD}>{quote.id}</td>
                      <td className={TD}>{names.hotels.get(quote.hotel_id) ?? quote.hotel_id}</td>
                      <td className={TD}>
                        {names.roomTypes.get(quote.room_type_id) ?? quote.room_type_id}
                      </td>
                      <td className={TD} dir="ltr">
                        {quote.check_in} → {quote.check_out}
                      </td>
                      <td className={TD}>{quote.rooms}</td>
                      <td className={TD}>{formatHalalasAsRiyal(quote.ask_price_total)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}

        {others.length > 0 && (
          <section className={CARD}>
            <h2 className={SECTION_TITLE}>تصعيدات أخرى لهذا العميل</h2>
            <ul className="mt-2 space-y-1">
              {others.map((other) => (
                <li key={other.id}>
                  <Link href={`/escalations/${other.id}`} className="underline">
                    رقم {other.id}
                  </Link>{" "}
                  — {escalationLabel(other.reason)} —{" "}
                  <span dir="ltr">{formatRiyadhDateTime(other.opened_at)}</span>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </AppShell>
  );
}
