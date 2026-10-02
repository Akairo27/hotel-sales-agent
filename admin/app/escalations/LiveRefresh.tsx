"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { createClient } from "@/utils/supabase/client";

// Live updates for the escalations screens (owner decision 8, 2026-10-01):
// Supabase Realtime on escalations, messages and, since migration 0034,
// conversation_takeovers -- so a takeover on one screen shows on every
// other -- and, since 0035, staff_replies, checked per subscriber against the tables' policies. An event's payload is never shown --
// it only triggers router.refresh(), so the page re-reads everything
// through the signed-in user's own RLS-scoped session. While the channel
// is not connected (still joining, timed out, closed or in error) the page
// polls instead.
const POLL_INTERVAL_MS = 30_000;
// Several changes in a burst (a reply stored right after the escalation
// that triggered it) cause one re-read, not several.
const REFRESH_DEBOUNCE_MS = 1_000;

export function LiveRefresh({
  channelName,
  conversationId,
}: {
  channelName: string;
  conversationId?: number;
}) {
  const router = useRouter();

  useEffect(() => {
    const supabase = createClient();
    let connected = false;
    let pending: ReturnType<typeof setTimeout> | undefined;

    const refreshSoon = () => {
      clearTimeout(pending);
      pending = setTimeout(() => router.refresh(), REFRESH_DEBOUNCE_MS);
    };

    const channel = supabase.channel(channelName);
    if (conversationId === undefined) {
      // The list: any escalation or takeover opened or changed.
      channel
        .on("postgres_changes", { event: "*", schema: "public", table: "escalations" }, refreshSoon)
        .on(
          "postgres_changes",
          { event: "*", schema: "public", table: "conversation_takeovers" },
          refreshSoon,
        );
    } else {
      // One customer's page: its conversation's escalations, messages,
      // takeovers and, since migration 0035, staff replies (a reply's sent
      // or failed outcome is an update of its row).
      const filter = `conversation_id=eq.${conversationId}`;
      channel
        .on("postgres_changes", { event: "*", schema: "public", table: "escalations", filter }, refreshSoon)
        .on("postgres_changes", { event: "INSERT", schema: "public", table: "messages", filter }, refreshSoon)
        .on(
          "postgres_changes",
          { event: "*", schema: "public", table: "conversation_takeovers", filter },
          refreshSoon,
        )
        .on(
          "postgres_changes",
          { event: "*", schema: "public", table: "staff_replies", filter },
          refreshSoon,
        );
    }
    channel.subscribe((status) => {
      connected = status === "SUBSCRIBED";
    });

    const poll = setInterval(() => {
      if (!connected) {
        router.refresh();
      }
    }, POLL_INTERVAL_MS);

    return () => {
      clearTimeout(pending);
      clearInterval(poll);
      void supabase.removeChannel(channel);
    };
  }, [channelName, conversationId, router]);

  return null;
}
