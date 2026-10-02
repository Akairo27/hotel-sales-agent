import Link from "next/link";
import { notFound, redirect } from "next/navigation";
import { AppShell } from "@/app/_components/AppShell";
import { messageSender, senderLabel, BUBBLE_CLASSES, SENDER_LABEL_CLASSES } from "@/lib/chatMessages";
import { isOpen, orderEscalations } from "@/lib/escalationCustomers";
import { escalationSummary } from "@/lib/escalationSummary";
import { escalationLabel, formatRiyadhDateTime, notOpenStays, parseNotes } from "@/lib/escalations";
import { getCurrentAppUser } from "@/lib/session";
import { noticeState } from "@/lib/takeoverNotice";
import type {
  ConversationRow,
  EscalationRow,
  MessageRow,
  QuoteSummaryRow,
} from "@/lib/types";
import { HINT, SECTION_TITLE } from "@/lib/ui";
import { createClient } from "@/utils/supabase/server";
import { LiveRefresh } from "../../LiveRefresh";
import { loadStayNames } from "../../_parts/names";
import { buildPanelQuotes } from "../../_parts/panelQuotes";
import { loadStaffReplyContext } from "../../_parts/staffReplies";
import { type ActiveTakeover, loadActiveTakeovers } from "../../_parts/takeovers";
import { ChatWorkspaceProvider, PanelToggleButton, SidePanel } from "./ChatWorkspace";
import { ConversationScroll } from "./ConversationScroll";
import { EscalationEntry } from "./EscalationEntry";
import { QuotePanel } from "./QuotePanel";
import { ReplyBox } from "./ReplyBox";
import { type PanelTakeover, TakeoverPanel } from "./TakeoverPanel";

// The latest messages of the conversation shown, oldest first.
const MESSAGE_LIMIT = 100;

const ESCALATION_COLUMNS =
  "id, conversation_id, customer_phone, reason, notes, quote_id, opened_at, " +
  "responded_at, resolved_at, assigned_to";
const QUOTE_COLUMNS =
  "id, hotel_id, room_type_id, check_in, check_out, rooms, ask_price_total, created_at";

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

function panelTakeover(takeover: ActiveTakeover | undefined): PanelTakeover | null {
  if (!takeover) {
    return null;
  }
  return {
    id: takeover.id,
    holderId: takeover.taken_over_by,
    holderName: takeover.holderName,
    takenOverAt: takeover.taken_over_at,
    noticeState: noticeState(takeover),
  };
}

function Bubble({ message, authorNames }: { message: MessageRow; authorNames: Map<number, string> }) {
  const sender = messageSender(message);
  return (
    <div className={BUBBLE_CLASSES[sender]}>
      <p className={`text-xs ${SENDER_LABEL_CLASSES[sender]}`}>
        {senderLabel(message, authorNames)} ·{" "}
        <span dir="ltr">{formatRiyadhDateTime(message.created_at)}</span>
      </p>
      <p className="mt-1 whitespace-pre-wrap break-words">{message.body}</p>
    </div>
  );
}

// One customer's conversation, chat first (staff notification steps 1, 2a
// and 3, ARCHITECTURE.md §7; grouped by customer, owner decision
// 2026-10-01; chat layout, owner request 2026-10-02): the conversation is
// the main column with the holder's reply box under it, a compact header
// carries the customer and take over, resolve and hand back, one pinned line
// says why the bot stopped, and the escalations and quotes sit in a side
// panel that collapses (a sheet on a narrow screen). Every read goes through
// the signed-in user's own session, so migrations 0033, 0034 and 0035's
// policies decide what exists here; customer text is rendered as text, never
// as HTML.
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
  const [
    conversationResult,
    escalationsResult,
    messagesResult,
    lastInboundResult,
    quotesResult,
    takeovers,
  ] = await Promise.all([
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
      .select("id, direction, body, created_at, staff_reply_id")
      .eq("conversation_id", conversationId)
      .order("created_at", { ascending: false })
      .order("id", { ascending: false })
      .limit(MESSAGE_LIMIT)
      .overrideTypes<MessageRow[], { merge: false }>(),
    // Apart from the messages shown: the 24-hour window counts from the
    // customer's last message, which a long run of replies could push out.
    supabase
      .from("messages")
      .select("created_at")
      .eq("conversation_id", conversationId)
      .eq("direction", "inbound")
      .order("created_at", { ascending: false })
      .limit(1)
      .maybeSingle<{ created_at: string }>(),
    supabase
      .from("quotes")
      .select(QUOTE_COLUMNS)
      .eq("conversation_id", conversationId)
      .order("created_at", { ascending: false })
      .overrideTypes<QuoteSummaryRow[], { merge: false }>(),
    loadActiveTakeovers(supabase, conversationId),
  ]);
  const conversation = conversationResult.data;
  const escalations = orderEscalations(escalationsResult.data ?? []);
  if (!conversation || escalations.length === 0) {
    notFound();
  }
  const messages = [...(messagesResult.data ?? [])].reverse();
  const staffReplies = await loadStaffReplyContext(
    supabase,
    conversationId,
    messages.flatMap((message) => (message.staff_reply_id === null ? [] : [message.staff_reply_id])),
  );
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
  const takeover = takeovers.byConversation.get(conversationId);
  const summary = escalationSummary(
    escalations,
    quotes,
    { hotel: (id) => names.hotels.get(id) ?? `الفندق ${id}` },
    now,
  );

  return (
    <AppShell appUser={appUser} layout="workspace">
      <LiveRefresh channelName={`escalations-customer-${conversationId}`} conversationId={conversationId} />
      <ChatWorkspaceProvider
        serverNow={now.toISOString()}
        canReply={takeover?.taken_over_by === appUser.id}
        lastInboundAt={lastInboundResult.data?.created_at ?? null}
      >
        <div className="flex min-h-0 flex-1">
          <section className="flex min-h-0 min-w-0 flex-1 flex-col" aria-label="محادثة العميل">
            <header className="shrink-0 border-b border-border bg-surface px-3 py-2 sm:px-4">
              <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1">
                <Link
                  href="/escalations"
                  className="text-sm text-muted-foreground transition hover:text-accent"
                >
                  → التصعيدات
                </Link>
                <a href={`tel:${conversation.customer_phone}`} dir="ltr" className="font-medium underline">
                  {conversation.customer_phone}
                </a>
                <span className={HINT}>
                  المفتوحة: {openCount} من {escalations.length}
                </span>
                <span className="ms-auto">
                  <PanelToggleButton />
                </span>
              </div>
              <div className="mt-2">
                <TakeoverPanel
                  conversationId={conversationId}
                  openCount={openCount}
                  takeover={panelTakeover(takeover)}
                  currentUserId={appUser.id}
                  isAdmin={appUser.app_role === "admin"}
                  now={now.toISOString()}
                  loadFailed={takeovers.failed}
                />
              </div>
            </header>
            <p
              title={summary}
              className="shrink-0 truncate border-b border-border bg-accent/5 px-3 py-1.5 text-sm sm:px-4"
            >
              <span className="text-muted-foreground">التصعيد: </span>
              {summary}
            </p>

            <ConversationScroll messageIds={messages.map((message) => message.id)}>
              {messages.length === MESSAGE_LIMIT && (
                <p className={`${HINT} mb-2 text-center`}>تظهر آخر {MESSAGE_LIMIT} رسالة فقط.</p>
              )}
              <ol className="space-y-2">
                {messages.map((message) => (
                  <li key={message.id} className="min-w-0">
                    {(before.get(message.id) ?? []).map((escalation) => (
                      <EscalationMarker key={escalation.id} escalation={escalation} />
                    ))}
                    <Bubble message={message} authorNames={staffReplies.authorNames} />
                  </li>
                ))}
              </ol>
              {after.map((escalation) => (
                <EscalationMarker key={escalation.id} escalation={escalation} />
              ))}
            </ConversationScroll>

            <ReplyBox
              conversationId={conversationId}
              takeoverId={takeover?.id ?? null}
              holderId={takeover?.taken_over_by ?? null}
              currentUserId={appUser.id}
              replies={staffReplies.recent.map((reply) => ({
                ...reply,
                authorName: staffReplies.authorNames.get(reply.id) ?? null,
              }))}
              loadFailed={staffReplies.failed}
            />
          </section>

          <SidePanel>
            <section>
              <h2 className={`${SECTION_TITLE} text-base`}>التصعيدات</h2>
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
            {quotes.length > 0 && (
              <section>
                <h2 className={`${SECTION_TITLE} text-base`}>العروض في هذه المحادثة</h2>
                <div className="mt-3">
                  <QuotePanel quotes={buildPanelQuotes(quotes, escalations, names, now)} />
                </div>
              </section>
            )}
          </SidePanel>
        </div>
      </ChatWorkspaceProvider>
    </AppShell>
  );
}
