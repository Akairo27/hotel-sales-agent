import type { TakeoverRow } from "@/lib/types";
import type { createClient } from "@/utils/supabase/server";

export interface ActiveTakeover extends TakeoverRow {
  holderName: string;
}

export interface ActiveTakeovers {
  /** Keyed by conversation id. */
  byConversation: Map<number, ActiveTakeover>;
  /** The read failed: the page must not show nobody holds anyone. */
  failed: boolean;
}

const TAKEOVER_COLUMNS =
  "id, conversation_id, taken_over_by, taken_over_at, ended_at, ack_claimed_at, " +
  "ack_sent_at, ack_failed_at";

// Shown if a holder's name cannot be read; migration 0034's view lists
// every staff member, deactivated ones included, so this is a fallback only.
const UNKNOWN_HOLDER = "موظف";

/** Every active takeover (staff notification step 2a, migration 0034) --
 * only conversationId's when given -- with its holder's name, read through
 * the signed-in user's own session. The list reads them all: few are
 * active at once, unlike the escalations behind them. */
export async function loadActiveTakeovers(
  supabase: Awaited<ReturnType<typeof createClient>>,
  conversationId?: number,
): Promise<ActiveTakeovers> {
  let query = supabase.from("conversation_takeovers").select(TAKEOVER_COLUMNS).is("ended_at", null);
  if (conversationId !== undefined) {
    query = query.eq("conversation_id", conversationId);
  }
  const { data, error } = await query.overrideTypes<TakeoverRow[], { merge: false }>();
  if (error) {
    return { byConversation: new Map(), failed: true };
  }
  const rows = data ?? [];
  const holderIds = [...new Set(rows.map((row) => row.taken_over_by))];
  const { data: names } =
    holderIds.length > 0
      ? await supabase.from("staff_names_for_dashboard").select("id, full_name").in("id", holderIds)
      : { data: [] };
  const nameById = new Map((names ?? []).map((row) => [row.id as string, row.full_name as string]));
  return {
    byConversation: new Map(
      rows.map((row) => [
        row.conversation_id,
        { ...row, holderName: nameById.get(row.taken_over_by) ?? UNKNOWN_HOLDER },
      ]),
    ),
    failed: false,
  };
}
