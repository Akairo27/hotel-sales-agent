import { redirect } from "next/navigation";
import { createClient } from "@/utils/supabase/server";
import { getCurrentAppUser } from "@/lib/session";
import type { Season } from "@/lib/types";
import { ALERT_ERROR, PAGE } from "@/lib/ui";
import { BackLink } from "@/app/_components/BackLink";
import { SeasonsWorkspace } from "./SeasonsWorkspace";

export default async function SeasonsPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const { error } = await searchParams;
  const appUser = await getCurrentAppUser();
  if (!appUser) {
    redirect("/login?error=" + encodeURIComponent("لا يوجد حساب مرتبط بهذا الدخول."));
  }

  const supabase = await createClient();
  const { data: seasons } = await supabase
    .from("seasons")
    .select(
      "id, season_name, calendar_type, start_month, start_day, end_month, end_day, " +
        "priority, is_default, created_at"
    )
    .overrideTypes<Season[], { merge: false }>();

  return (
    <main className={PAGE}>
      <BackLink href="/dashboard">لوحة التحكم</BackLink>
      <h1 className="text-2xl font-semibold text-foreground sm:text-3xl">المواسم</h1>

      {error && (
        <p role="alert" className={`${ALERT_ERROR} mt-4`}>
          {error}
        </p>
      )}

      <div className="mt-8">
        <SeasonsWorkspace initialSeasons={seasons ?? []} isAdmin={appUser.app_role === "admin"} />
      </div>
    </main>
  );
}
