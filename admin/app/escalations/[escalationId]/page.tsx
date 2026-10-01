import { notFound, redirect } from "next/navigation";
import { customerPageFor } from "@/lib/escalationCustomers";
import { getCurrentAppUser } from "@/lib/session";
import { createClient } from "@/utils/supabase/server";

// An escalation's own address (old bookmarks, and step 2's alert links,
// whose approved text names an escalation number) opens its customer's page
// at that escalation: the screen is grouped by customer (owner decision
// 2026-10-01). Read through the signed-in user's own session, so an
// escalation they may not see does not exist here either.
export default async function EscalationRedirect({
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
  const { data } = await supabase
    .from("escalations")
    .select("id, conversation_id")
    .eq("id", id)
    .maybeSingle<{ id: number; conversation_id: number }>();
  if (!data) {
    notFound();
  }
  redirect(customerPageFor(data.conversation_id, data.id));
}
