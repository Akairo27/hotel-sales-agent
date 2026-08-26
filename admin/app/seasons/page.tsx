import { redirect } from "next/navigation";
import { createClient } from "@/utils/supabase/server";
import { getCurrentAppUser } from "@/lib/session";
import type { Season } from "@/lib/types";
import { ALERT_ERROR } from "@/lib/ui";
import { AppShell } from "@/app/_components/AppShell";
import { PageHeader } from "@/app/_components/PageHeader";
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
    <AppShell appUser={appUser}>
      <PageHeader
        title="المواسم"
        description="تقويم المواسم وأولوياتها، مع معاينة التغطية لسنة هجرية كاملة."
      />

      {error && (
        <p role="alert" className={`${ALERT_ERROR} mb-6`}>
          {error}
        </p>
      )}

      <SeasonsWorkspace initialSeasons={seasons ?? []} isAdmin={appUser.app_role === "admin"} />
    </AppShell>
  );
}
