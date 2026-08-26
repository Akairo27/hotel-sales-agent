import { redirect } from "next/navigation";
import { createClient } from "@/utils/supabase/server";
import { getCurrentAppUser } from "@/lib/session";
import type { Hotel, PriceRuleForDashboard, RoomType, Season } from "@/lib/types";
import { ALERT_STATUS } from "@/lib/ui";
import { AppShell } from "@/app/_components/AppShell";
import { PageHeader } from "@/app/_components/PageHeader";
import { PriceRulesWorkspace } from "./PriceRulesWorkspace";

export default async function PriceRulesPage() {
  const appUser = await getCurrentAppUser();
  if (!appUser) {
    redirect("/login?error=" + encodeURIComponent("لا يوجد حساب مرتبط بهذا الدخول."));
  }

  const supabase = await createClient();
  const [{ data: rules }, { data: hotels }, { data: roomTypes }, { data: seasons }] =
    await Promise.all([
      supabase
        .from("price_rules_for_dashboard")
        .select(
          "id, scope, scope_id, demand_curve, created_at, is_active, " +
            "target_margin_bps, min_profit_by_lead_time"
        )
        .order("scope")
        .overrideTypes<PriceRuleForDashboard[], { merge: false }>(),
      supabase.from("hotels").select("id, hotel_name, created_at").overrideTypes<
        Hotel[],
        { merge: false }
      >(),
      supabase.from("room_types").select("id, hotel_id, room_type_name, created_at").overrideTypes<
        RoomType[],
        { merge: false }
      >(),
      supabase
        .from("seasons")
        .select(
          "id, season_name, calendar_type, start_month, start_day, end_month, " +
            "end_day, priority, is_default, created_at"
        )
        .overrideTypes<Season[], { merge: false }>(),
    ]);

  const canEdit = appUser.app_role === "admin" && appUser.can_view_cost;

  return (
    <AppShell appUser={appUser}>
      <PageHeader
        title="قواعد التسعير"
        description="قواعد الهامش ومنحنى الطلب، محلولة حسب النطاق الأضيق أولاً."
      />

      {!appUser.can_view_cost && (
        <p className={`${ALERT_STATUS} mb-6`}>
          لا تملك صلاحية عرض التكلفة — الهامش وحد الربح الأدنى مخفيان عنك.
        </p>
      )}

      <PriceRulesWorkspace
        initialRules={rules ?? []}
        hotels={hotels ?? []}
        roomTypes={roomTypes ?? []}
        seasons={seasons ?? []}
        canEdit={canEdit}
      />
    </AppShell>
  );
}
