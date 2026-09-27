import { redirect } from "next/navigation";
import { createClient } from "@/utils/supabase/server";
import { getCurrentAppUser } from "@/lib/session";
import { halalasToRiyals } from "@/lib/money";
import type {
  AllotmentForDashboard,
  HotelRef,
  RoomNightAvailabilityForDashboard,
  RoomTypeRef,
} from "@/lib/types";
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
import { AllotmentEntryForm } from "./AllotmentEntryForm";
import { updateAllotmentNight } from "./actions";

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
  const [{ data: allotments }, { data: hotels }, { data: roomTypes }, { data: availability }] =
    await Promise.all([
      supabase
        .from("allotments_for_dashboard")
        .select("id, hotel_id, room_type_id, stay_date, total_rooms, cost_per_night, created_at")
        .order("stay_date")
        .overrideTypes<AllotmentForDashboard[], { merge: false }>(),
      supabase.from("hotels").select("id, hotel_name, created_at").overrideTypes<
        HotelRef[],
        { merge: false }
      >(),
      supabase.from("room_types").select("id, hotel_id, room_type_name, created_at").overrideTypes<
        RoomTypeRef[],
        { merge: false }
      >(),
      supabase
        .from("room_night_availability_for_dashboard")
        .select("allotment_id, stay_date, total, reserved, held")
        .overrideTypes<RoomNightAvailabilityForDashboard[], { merge: false }>(),
    ]);

  const hotelNames = new Map((hotels ?? []).map((h) => [h.id, h.hotel_name]));
  const roomTypeNames = new Map((roomTypes ?? []).map((rt) => [rt.id, rt.room_type_name]));
  const availabilityByAllotmentId = new Map(
    (availability ?? []).map((row) => [row.allotment_id, row])
  );
  const canEditCost = appUser.app_role === "admin" && appUser.can_view_cost;

  return (
    <AppShell appUser={appUser}>
      <PageHeader title="التكلفة" description="عدد الغرف وتكلفة الليلة لكل نوع غرفة في كل تاريخ." />

      {error && (
        <p role="alert" className={`${ALERT_ERROR} mb-6`}>
          {error}
        </p>
      )}
      {!appUser.can_view_cost && (
        <p className={`${ALERT_STATUS} mb-6`}>لا تملك صلاحية عرض التكلفة — راجع شاشة الصلاحيات.</p>
      )}

      {canEditCost && (
        <div className="mb-8">
          <AllotmentEntryForm hotels={hotels ?? []} roomTypes={roomTypes ?? []} />
        </div>
      )}

      <div className={TABLE_WRAPPER}>
        <table className={TABLE}>
          <thead>
            <tr>
              <th className={TH}>الفندق</th>
              <th className={TH}>نوع الغرفة</th>
              <th className={TH}>التاريخ</th>
              <th className={TH}>عدد الغرف</th>
              <th className={TH}>التكلفة لليلة (ريال)</th>
              <th className={TH}>محجوز</th>
              <th className={TH}>حجز مؤقت</th>
            </tr>
          </thead>
          <tbody>
            {(allotments ?? []).map((allotment) => {
              const nightAvailability = availabilityByAllotmentId.get(allotment.id);
              const formId = `night-form-${allotment.id}`;
              const updateThisNight = updateAllotmentNight.bind(
                null,
                allotment.hotel_id,
                allotment.room_type_id,
                allotment.stay_date
              );
              const costRiyals =
                allotment.cost_per_night !== null ? halalasToRiyals(allotment.cost_per_night) : null;
              return (
                <tr key={allotment.id} className={TABLE_ROW}>
                  <td className={TD}>
                    {hotelNames.get(allotment.hotel_id) ?? allotment.hotel_id}
                  </td>
                  <td className={TD}>
                    {roomTypeNames.get(allotment.room_type_id) ?? allotment.room_type_id}
                  </td>
                  <td className={TD}>{allotment.stay_date}</td>
                  <td className={TD}>
                    {canEditCost ? (
                      <>
                        <label htmlFor={`rooms-${allotment.id}`} className="sr-only">
                          عدد الغرف
                        </label>
                        <input
                          id={`rooms-${allotment.id}`}
                          form={formId}
                          name="total_rooms"
                          type="number"
                          min={0}
                          step={1}
                          defaultValue={allotment.total_rooms}
                          required
                          className={`${INPUT} w-24`}
                        />
                      </>
                    ) : (
                      allotment.total_rooms
                    )}
                  </td>
                  <td className={TD}>
                    {canEditCost ? (
                      <div className="flex items-center gap-2">
                        {/* Empty on purpose — the room-count input above and
                            the cost input and button below all reference
                            this form by id, since a single <form> element
                            cannot itself span more than one <td>. */}
                        <form id={formId} action={updateThisNight} />
                        <label htmlFor={`cost-${allotment.id}`} className="sr-only">
                          التكلفة لليلة (ريال)
                        </label>
                        <input
                          id={`cost-${allotment.id}`}
                          form={formId}
                          name="cost_per_night_riyals"
                          type="number"
                          min={0}
                          step={1}
                          defaultValue={costRiyals ?? undefined}
                          required
                          className={`${INPUT} w-32`}
                        />
                        <button form={formId} type="submit" className={BUTTON_SECONDARY}>
                          حفظ
                        </button>
                      </div>
                    ) : (
                      (costRiyals ?? "—")
                    )}
                  </td>
                  <td className={TD}>{nightAvailability?.reserved ?? "—"}</td>
                  <td className={TD}>{nightAvailability?.held ?? "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </AppShell>
  );
}
