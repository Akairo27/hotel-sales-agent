"use server";

import { redirect } from "next/navigation";
import { createClient } from "@/utils/supabase/server";

// Signing out is a Server Action rather than a route handler so the shell's
// button is a plain form submit — no client-side Supabase client, and the
// auth cookies are cleared by the same request that redirects.
export async function logout(): Promise<void> {
  const supabase = await createClient();
  const { error } = await supabase.auth.signOut();
  if (error) {
    // The session cookies survived, so middleware would bounce a bare
    // redirect straight back to /dashboard. Say so instead of appearing
    // to sign out and silently not doing it.
    redirect("/login?error=" + encodeURIComponent(error.message));
  }

  redirect("/login");
}
