import type { StaffReplyRow } from "@/lib/types";
import type { createClient } from "@/utils/supabase/server";

export interface StaffReplyContext {
  /** The conversation's latest replies, newest last, for the reply box. */
  recent: StaffReplyRow[];
  /** Author name by staff reply id, for the replies behind the messages
   * shown and the recent ones. A reply whose author's name could not be
   * read is absent. */
  authorNames: Map<number, string>;
  /** The read failed: the page must not show there are no replies. */
  failed: boolean;
}

// How many of the latest replies the reply box lists.
const RECENT_REPLY_LIMIT = 10;

const REPLY_COLUMNS =
  "id, takeover_id, conversation_id, sent_by, body, created_at, claimed_at, sent_at, " +
  "failed_at, failure_reason";

/** The conversation's latest staff replies and the authors of every staff
 * reply the shown messages carry (migration 0035), read through the
 * signed-in user's own session; names come from migration 0034's view. */
export async function loadStaffReplyContext(
  supabase: Awaited<ReturnType<typeof createClient>>,
  conversationId: number,
  shownReplyIds: number[],
): Promise<StaffReplyContext> {
  const [recentResult, shownResult] = await Promise.all([
    supabase
      .from("staff_replies")
      .select(REPLY_COLUMNS)
      .eq("conversation_id", conversationId)
      .order("created_at", { ascending: false })
      .order("id", { ascending: false })
      .limit(RECENT_REPLY_LIMIT)
      .overrideTypes<StaffReplyRow[], { merge: false }>(),
    shownReplyIds.length > 0
      ? supabase
          .from("staff_replies")
          .select("id, sent_by")
          .in("id", shownReplyIds)
          .overrideTypes<Pick<StaffReplyRow, "id" | "sent_by">[], { merge: false }>()
      : Promise.resolve({ data: [], error: null }),
  ]);
  if (recentResult.error || shownResult.error) {
    return { recent: [], authorNames: new Map(), failed: true };
  }
  const recent = [...(recentResult.data ?? [])].reverse();
  const authorById = new Map(
    [...recent, ...(shownResult.data ?? [])].map((reply) => [reply.id, reply.sent_by]),
  );
  const authorIds = [...new Set(authorById.values())];
  const { data: names } =
    authorIds.length > 0
      ? await supabase.from("staff_names_for_dashboard").select("id, full_name").in("id", authorIds)
      : { data: [] };
  const nameByUserId = new Map((names ?? []).map((row) => [row.id as string, row.full_name as string]));
  const authorNames = new Map<number, string>();
  for (const [replyId, authorId] of authorById) {
    const name = nameByUserId.get(authorId);
    if (name !== undefined) {
      authorNames.set(replyId, name);
    }
  }
  return { recent, authorNames, failed: false };
}
