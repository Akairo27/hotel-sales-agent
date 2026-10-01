"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { createClient } from "@/utils/supabase/client";

// Live updates for the escalations screens (owner decision 8, 2026-10-01):
// Supabase Realtime on escalations and messages, checked per subscriber
// against migration 0033's policies. An event's payload is never shown --
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
      // The list: any escalation opened or changed.
      channel.on("postgres_changes", { event: "*", schema: "public", table: "escalations" }, refreshSoon);
    } else {
      // One escalation's page: its conversation's escalations and messages.
      const filter = `conversation_id=eq.${conversationId}`;
      channel
        .on("postgres_changes", { event: "*", schema: "public", table: "escalations", filter }, refreshSoon)
        .on("postgres_changes", { event: "INSERT", schema: "public", table: "messages", filter }, refreshSoon);
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
