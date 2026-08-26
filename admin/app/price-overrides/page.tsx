import { redirect } from "next/navigation";
import { createClient } from "@/utils/supabase/server";
import { getCurrentAppUser } from "@/lib/session";
import type { Hotel, PriceOverride, RoomType } from "@/lib/types";
import { PAGE } from "@/lib/ui";
import { BackLink } from "@/app/_components/BackLink";
import { PriceOverridesWorkspace } from "./PriceOverridesWorkspace";

export default async function PriceOverridesPage() {
  const appUser = await getCurrentAppUser();
  if (!appUser) {
    redirect("/login?error=" + encodeURIComponent("لا يوجد حساب مرتبط بهذا الدخول."));
  }

  const supabase = await createClient();
  const [{ data: overrides }, { data: hotels }, { data: roomTypes }] = await Promise.all([
    supabase
      .from("price_overrides")
      .select(
        "id, hotel_id, room_type_id, stay_date, ask_price_override, " +
          "min_allowed_override, expires_at, created_at"
      )
      .order("hotel_id")
      .order("room_type_id")
      .order("stay_date")
      .overrideTypes<PriceOverride[], { merge: false }>(),
    supabase.from("hotels").select("id, hotel_name, created_at").overrideTypes<
      Hotel[],
      { merge: false }
    >(),
    supabase.from("room_types").select("id, hotel_id, room_type_name, created_at").overrideTypes<
      RoomType[],
      { merge: false }
    >(),
  ]);

  // No can_view_cost condition here, unlike price-rules — these three
  // columns are final prices, not a margin or profit floor over cost.
  const canEdit = appUser.app_role === "admin";

  return (
    <main className={PAGE}>
      <BackLink href="/dashboard">لوحة التحكم</BackLink>
      <h1 className="text-2xl font-semibold text-foreground sm:text-3xl">تجاوزات الأسعار</h1>

      <div className="mt-8">
        <PriceOverridesWorkspace
          initialOverrides={overrides ?? []}
          hotels={hotels ?? []}
          roomTypes={roomTypes ?? []}
          canEdit={canEdit}
        />
      </div>
    </main>
  );
}
