import { redirect } from "next/navigation";
import { createClient } from "@/utils/supabase/server";
import { getCurrentAppUser } from "@/lib/session";
import type { AllotmentForDashboard, Hotel, RoomType } from "@/lib/types";
import {
  ALERT_ERROR,
  ALERT_STATUS,
  BUTTON_SECONDARY,
  INPUT,
  TABLE,
  TABLE_ROW,
  TABLE_WRAPPER,
  TD,
  TH,
} from "@/lib/ui";
import { AppShell } from "@/app/_components/AppShell";
import { PageHeader } from "@/app/_components/PageHeader";
import { updateAllotmentCost } from "./actions";

export default async function AllotmentsPage({
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
  const [{ data: allotments }, { data: hotels }, { data: roomTypes }] = await Promise.all([
    supabase
      .from("allotments_for_dashboard")
      .select("id, hotel_id, room_type_id, stay_date, total_rooms, cost_per_night, created_at")
      .order("stay_date")
      .overrideTypes<AllotmentForDashboard[], { merge: false }>(),
    supabase.from("hotels").select("id, hotel_name, created_at").overrideTypes<
      Hotel[],
      { merge: false }
    >(),
    supabase.from("room_types").select("id, hotel_id, room_type_name, created_at").overrideTypes<
      RoomType[],
      { merge: false }
    >(),
  ]);

  const hotelNames = new Map((hotels ?? []).map((h) => [h.id, h.hotel_name]));
  const roomTypeNames = new Map((roomTypes ?? []).map((rt) => [rt.id, rt.room_type_name]));
  const canEditCost = appUser.app_role === "admin" && appUser.can_view_cost;

  return (
    <AppShell appUser={appUser}>
      <PageHeader title="التكلفة" description="تكلفة الليلة لكل نوع غرفة في كل تاريخ." />

      {error && (
        <p role="alert" className={`${ALERT_ERROR} mb-6`}>
          {error}
        </p>
      )}
      {!appUser.can_view_cost && (
        <p className={`${ALERT_STATUS} mb-6`}>لا تملك صلاحية عرض التكلفة — راجع شاشة الصلاحيات.</p>
      )}

      <div className={TABLE_WRAPPER}>
        <table className={TABLE}>
          <thead>
            <tr>
              <th className={TH}>الفندق</th>
              <th className={TH}>نوع الغرفة</th>
              <th className={TH}>التاريخ</th>
              <th className={TH}>عدد الغرف</th>
              <th className={TH}>التكلفة لليلة (هللة)</th>
            </tr>
          </thead>
          <tbody>
            {(allotments ?? []).map((allotment) => (
              <tr key={allotment.id} className={TABLE_ROW}>
                <td className={TD}>{hotelNames.get(allotment.hotel_id) ?? allotment.hotel_id}</td>
                <td className={TD}>
                  {roomTypeNames.get(allotment.room_type_id) ?? allotment.room_type_id}
                </td>
                <td className={TD}>{allotment.stay_date}</td>
                <td className={TD}>{allotment.total_rooms}</td>
                <td className={TD}>
                  {canEditCost ? (
                    <CostForm allotment={allotment} />
                  ) : (
                    (allotment.cost_per_night ?? "—")
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </AppShell>
  );
}

function CostForm({ allotment }: { allotment: AllotmentForDashboard }) {
  const updateThisAllotmentsCost = updateAllotmentCost.bind(null, allotment.id);
  return (
    <form action={updateThisAllotmentsCost} className="flex items-center gap-2">
      <label htmlFor={`cost-${allotment.id}`} className="sr-only">
        التكلفة لليلة (هللة)
      </label>
      <input
        id={`cost-${allotment.id}`}
        name="cost_per_night"
        type="number"
        min={0}
        step={1}
        defaultValue={allotment.cost_per_night ?? undefined}
        required
        className={`${INPUT} w-32`}
      />
      <button type="submit" className={BUTTON_SECONDARY}>
        حفظ
      </button>
    </form>
  );
}
