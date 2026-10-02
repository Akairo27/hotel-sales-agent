"use server";

import { revalidatePath } from "next/cache";
import { requestTakeoverNotice } from "@/lib/agentInternal";
import { customerPage } from "@/lib/escalationCustomers";
import { getCurrentAppUser } from "@/lib/session";
import type { TakeoverNoticeStatus } from "@/lib/takeoverNotice";
import type { TakeOverResultRow, TakeoverRow } from "@/lib/types";
import { createClient } from "@/utils/supabase/server";

// Staff notification step 2a (owner decisions 2026-10-02): take over,
// resolve and hand back a customer's conversation. Every write goes through
// the signed-in user's own session to migration 0034's functions, whose
// policies decide who may do what; the checks here only turn a refusal into
// a message. Render-time gating is not a security boundary on its own.

const ESCALATIONS_PATH = "/escalations";

const NOT_PERMITTED = "ليست لديك صلاحية لهذا الإجراء.";
const INVALID_REQUEST = "طلب غير صالح.";
const STATE_CHANGED = "تغيّرت حالة المحادثة. أعد تحميل الصفحة وحاول مرة أخرى.";

// The SQLSTATEs migration 0034's functions raise, as PostgREST reports them.
const SQLSTATE_NOT_PERMITTED = "42501";
const SQLSTATE_NOTHING_TO_ACT_ON = "P0002";

export type TakeOverResult =
  | { outcome: "won"; notice: TakeoverNoticeStatus }
  | { outcome: "already_yours" }
  | { outcome: "lost"; holderName: string }
  | { outcome: "error"; message: string };

export type CloseOutcome = "resolved" | "handed_back";

export type CloseResult = { closed: number } | { error: string };

export type NoticeResult = { notice: TakeoverNoticeStatus } | { error: string };

function isId(value: number): boolean {
  return Number.isSafeInteger(value) && value > 0;
}

function revalidate(conversationId: number): void {
  revalidatePath(customerPage(conversationId));
  revalidatePath(ESCALATIONS_PATH);
}

async function staffName(
  supabase: Awaited<ReturnType<typeof createClient>>,
  userId: string,
): Promise<string> {
  const { data } = await supabase
    .from("staff_names_for_dashboard")
    .select("full_name")
    .eq("id", userId)
    .maybeSingle<{ full_name: string }>();
  return data?.full_name ?? "موظف آخر";
}

/** Takes the conversation over for the signed-in staff member. Exactly one
 * of two people pressing at once wins (migration 0034's partial unique
 * index); the other is told who did. A win then asks the agent for the
 * notice to the customer, whose outcome is returned with it. */
export async function takeOverConversation(conversationId: number): Promise<TakeOverResult> {
  if (!isId(conversationId)) {
    return { outcome: "error", message: INVALID_REQUEST };
  }
  const appUser = await getCurrentAppUser();
  if (!appUser?.is_active) {
    return { outcome: "error", message: NOT_PERMITTED };
  }
  const supabase = await createClient();
  const { data, error } = await supabase.rpc("staff_take_over_conversation", {
    target_conversation_id: conversationId,
  });
  if (error) {
    const message =
      error.code === SQLSTATE_NOTHING_TO_ACT_ON
        ? "لا توجد تصعيدات مفتوحة لهذا العميل، فلا يوجد ما يُستلم."
        : error.code === SQLSTATE_NOT_PERMITTED
          ? NOT_PERMITTED
          : "تعذّر الاستلام. حاول مرة أخرى.";
    return { outcome: "error", message };
  }
  // Same honest cast as admin/app/allotments/actions.ts: this client has no
  // Database generic, so .rpc()'s result is untyped.
  const [row] = (data as unknown as TakeOverResultRow[] | null) ?? [];
  revalidate(conversationId);
  if (!row) {
    return { outcome: "error", message: STATE_CHANGED };
  }
  if (!row.won) {
    return row.holder_id === appUser.id
      ? { outcome: "already_yours" }
      : { outcome: "lost", holderName: await staffName(supabase, row.holder_id) };
  }
  return { outcome: "won", notice: await requestTakeoverNotice(row.takeover_id) };
}

function closeErrorMessage(code: string | undefined, closeOutcome: CloseOutcome): string {
  if (code === SQLSTATE_NOT_PERMITTED) {
    return "استلم هذه المحادثة موظف آخر، وإنهاؤها له أو للمدير.";
  }
  if (code === SQLSTATE_NOTHING_TO_ACT_ON) {
    return closeOutcome === "handed_back"
      ? "المحادثة غير مستلمة الآن، فلا يوجد ما يُعاد للبوت."
      : STATE_CHANGED;
  }
  return "تعذّر تنفيذ الطلب. حاول مرة أخرى.";
}

/** Resolves the conversation or hands it back to the bot (owner decision
 * D2): ends the takeover, if any, and closes every open escalation. Sends
 * the customer nothing. Returns how many escalations were closed. */
export async function closeConversation(
  conversationId: number,
  closeOutcome: CloseOutcome,
): Promise<CloseResult> {
  if (!isId(conversationId) || (closeOutcome !== "resolved" && closeOutcome !== "handed_back")) {
    return { error: INVALID_REQUEST };
  }
  const appUser = await getCurrentAppUser();
  if (!appUser?.is_active) {
    return { error: NOT_PERMITTED };
  }
  const supabase = await createClient();
  const { data, error } = await supabase.rpc("staff_close_conversation", {
    target_conversation_id: conversationId,
    close_outcome: closeOutcome,
  });
  if (error) {
    return { error: closeErrorMessage(error.code, closeOutcome) };
  }
  revalidate(conversationId);
  return { closed: (data as unknown as number | null) ?? 0 };
}

/** Asks the agent again for a takeover's notice, after the first attempt
 * could not reach it. Only the holder or an admin, and only while the
 * takeover is active; the agent itself sends it at most once. */
export async function sendTakeoverNotice(takeoverId: number): Promise<NoticeResult> {
  if (!isId(takeoverId)) {
    return { error: INVALID_REQUEST };
  }
  const appUser = await getCurrentAppUser();
  if (!appUser?.is_active) {
    return { error: NOT_PERMITTED };
  }
  const supabase = await createClient();
  const { data: takeover } = await supabase
    .from("conversation_takeovers")
    .select("id, conversation_id, taken_over_by, ended_at")
    .eq("id", takeoverId)
    .maybeSingle<Pick<TakeoverRow, "id" | "conversation_id" | "taken_over_by" | "ended_at">>();
  if (!takeover || takeover.ended_at !== null) {
    return { error: STATE_CHANGED };
  }
  if (takeover.taken_over_by !== appUser.id && appUser.app_role !== "admin") {
    return { error: NOT_PERMITTED };
  }
  const notice = await requestTakeoverNotice(takeoverId);
  revalidate(takeover.conversation_id);
  return { notice };
}
