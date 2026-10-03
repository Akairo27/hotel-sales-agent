"use server";

import { revalidatePath } from "next/cache";
import { requestReengagementTemplateEnabled, requestStaffReplySend } from "@/lib/agentInternal";
import { customerPage } from "@/lib/escalationCustomers";
import { reengagementHotelId } from "@/lib/reengagementHotel";
import { getCurrentAppUser } from "@/lib/session";
import {
  type StaffReplyStatus,
  TEMPLATE_NOT_ENABLED,
  isWithinCustomerServiceWindow,
  replyDraftProblem,
} from "@/lib/staffReply";
import type { EscalationRow, QuoteSummaryRow, StaffReplyRow } from "@/lib/types";
import { createClient } from "@/utils/supabase/server";
import { ESCALATION_COLUMNS } from "./_parts/columns";

// Staff notification step 3 (owner decisions 2026-10-02): the holder of a
// takeover writes to the customer. The reply is stored through the
// signed-in user's own session (migration 0035's staff_queue_reply, whose
// policies decide who may write), then the agent is asked to send it by id.
// The agent sends the stored row's body, never text from here, and runs the
// output guard's staff-reply mode and the amounts audit before the send.

const ESCALATIONS_PATH = "/escalations";

const NOT_PERMITTED = "ليست لديك صلاحية لهذا الإجراء.";
const INVALID_REQUEST = "طلب غير صالح.";
const NOT_HOLDER = "هذه المحادثة ليست مستلمة باسمك، فلا يمكنك الرد منها.";
const NOT_TAKEN_OVER = "المحادثة غير مستلمة الآن. أعد تحميل الصفحة.";
const STATE_CHANGED = "تغيّرت حالة المحادثة. أعد تحميل الصفحة وحاول مرة أخرى.";

// The SQLSTATEs migration 0035's function raises, as PostgREST reports them.
const SQLSTATE_NOT_PERMITTED = "42501";
const SQLSTATE_NOT_TAKEN_OVER = "P0002";

export type ReplyResult =
  | { replyId: number; status: StaffReplyStatus }
  | { error: string };

function isId(value: number): boolean {
  return Number.isSafeInteger(value) && value > 0;
}

function revalidate(conversationId: number): void {
  revalidatePath(customerPage(conversationId));
  revalidatePath(ESCALATIONS_PATH);
}

function queueErrorMessage(code: string | undefined): string {
  if (code === SQLSTATE_NOT_PERMITTED) {
    return NOT_HOLDER;
  }
  if (code === SQLSTATE_NOT_TAKEN_OVER) {
    return NOT_TAKEN_OVER;
  }
  return "تعذّر حفظ الرد. حاول مرة أخرى.";
}

/** Stores the signed-in staff member's reply to a conversation they hold,
 * then asks the agent to send it. Returns the reply's id with the agent's
 * status; an "unreachable" or "unavailable" status leaves the reply stored
 * and unsent, for retryStaffReply. */
export async function sendStaffReply(conversationId: number, body: string): Promise<ReplyResult> {
  if (!isId(conversationId) || typeof body !== "string") {
    return { error: INVALID_REQUEST };
  }
  const problem = replyDraftProblem(body);
  if (problem !== null) {
    return { error: problem };
  }
  const appUser = await getCurrentAppUser();
  if (!appUser?.is_active) {
    return { error: NOT_PERMITTED };
  }
  const supabase = await createClient();
  const { data, error } = await supabase.rpc("staff_queue_reply", {
    target_conversation_id: conversationId,
    reply_body: body,
  });
  if (error) {
    return { error: queueErrorMessage(error.code) };
  }
  const replyId = data as unknown as number | null;
  if (replyId === null || !isId(replyId)) {
    return { error: STATE_CHANGED };
  }
  const status = await requestStaffReplySend(replyId);
  revalidate(conversationId);
  return { replyId, status };
}

const WINDOW_OPEN =
  "نافذة الـ24 ساعة مفتوحة، فلا حاجة لقالب إعادة التواصل. اكتب ردك مباشرة.";

type SupabaseClient = Awaited<ReturnType<typeof createClient>>;

/** The hotel the template names for this conversation (owner decision
 * 2026-10-02; lib/reengagementHotel.ts), read through the user's session. */
async function templateHotelId(
  supabase: SupabaseClient,
  conversationId: number,
): Promise<number | null> {
  const [{ data: quotes }, { data: escalations }] = await Promise.all([
    supabase
      .from("quotes")
      .select("id, hotel_id, created_at")
      .eq("conversation_id", conversationId)
      .overrideTypes<Pick<QuoteSummaryRow, "id" | "hotel_id" | "created_at">[], { merge: false }>(),
    supabase
      .from("escalations")
      .select(ESCALATION_COLUMNS)
      .eq("conversation_id", conversationId)
      .overrideTypes<EscalationRow[], { merge: false }>(),
  ]);
  return reengagementHotelId(quotes ?? [], escalations ?? []);
}

/** Whether the customer wrote within the 24-hour window, from their latest
 * message through the user's session. A failed read counts as open: the
 * template is refused rather than sent on a guess (the agent checks again,
 * by the database clock). */
async function windowIsOpen(supabase: SupabaseClient, conversationId: number): Promise<boolean> {
  const { data, error } = await supabase
    .from("messages")
    .select("created_at")
    .eq("conversation_id", conversationId)
    .eq("direction", "inbound")
    .order("created_at", { ascending: false })
    .limit(1)
    .maybeSingle<{ created_at: string }>();
  return error !== null || isWithinCustomerServiceWindow(data?.created_at ?? null, new Date());
}

/** Sends the re-engagement template to a customer whose 24-hour window has
 * closed (staff notification step 3, PR C). The holder only, as for a text
 * reply: stored through staff_queue_template_reply on the user's session,
 * then the agent is asked to send it by id. Refused here, before anything is
 * stored, while the window is open or the agent has no template names; the
 * agent checks both again. */
export async function sendReengagementTemplate(conversationId: number): Promise<ReplyResult> {
  if (!isId(conversationId)) {
    return { error: INVALID_REQUEST };
  }
  const appUser = await getCurrentAppUser();
  if (!appUser?.is_active) {
    return { error: NOT_PERMITTED };
  }
  const supabase = await createClient();
  if (await windowIsOpen(supabase, conversationId)) {
    return { error: WINDOW_OPEN };
  }
  if (!(await requestReengagementTemplateEnabled())) {
    return { error: TEMPLATE_NOT_ENABLED };
  }
  const { data, error } = await supabase.rpc("staff_queue_template_reply", {
    target_conversation_id: conversationId,
    hotel_for_template: await templateHotelId(supabase, conversationId),
  });
  if (error) {
    return { error: queueErrorMessage(error.code) };
  }
  const replyId = data as unknown as number | null;
  if (replyId === null || !isId(replyId)) {
    return { error: STATE_CHANGED };
  }
  const status = await requestStaffReplySend(replyId);
  revalidate(conversationId);
  return { replyId, status };
}

/** Asks the agent again to send a reply it could not be asked, or could not
 * claim, the first time. Only its author, and only while it is unsent: the
 * agent claims a reply at most once, so a reply it already took is never
 * sent twice. */
export async function retryStaffReply(replyId: number): Promise<ReplyResult> {
  if (!isId(replyId)) {
    return { error: INVALID_REQUEST };
  }
  const appUser = await getCurrentAppUser();
  if (!appUser?.is_active) {
    return { error: NOT_PERMITTED };
  }
  const supabase = await createClient();
  const { data: reply } = await supabase
    .from("staff_replies")
    .select("id, conversation_id, sent_by")
    .eq("id", replyId)
    .maybeSingle<Pick<StaffReplyRow, "id" | "conversation_id" | "sent_by">>();
  if (!reply) {
    return { error: STATE_CHANGED };
  }
  if (reply.sent_by !== appUser.id) {
    return { error: NOT_PERMITTED };
  }
  const status = await requestStaffReplySend(replyId);
  revalidate(reply.conversation_id);
  return { replyId, status };
}
