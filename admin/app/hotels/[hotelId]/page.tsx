import { notFound, redirect } from "next/navigation";
import { createClient } from "@/utils/supabase/server";
import { getCurrentAppUser } from "@/lib/session";
import type { Hotel, HotelAmenityRow, RoomType } from "@/lib/types";
import type { HotelAmenity } from "@/lib/hotelDetails";
import { missingHotelProfileFields } from "@/lib/hotelDetails";
import { ALERT_ERROR, ALERT_STATUS, BUTTON_PRIMARY, CARD, INPUT, LABEL, SECTION_TITLE } from "@/lib/ui";
import { AppShell } from "@/app/_components/AppShell";
import { PageHeader } from "@/app/_components/PageHeader";
import { HotelDetailsForm } from "./HotelDetailsForm";
import { RoomTypeCard } from "./RoomTypeCard";
import { createRoomType } from "./actions";

export default async function HotelProfilePage({
  params,
  searchParams,
}: {
  params: Promise<{ hotelId: string }>;
  searchParams: Promise<{ error?: string }>;
}) {
  const { hotelId } = await params;
  const { error } = await searchParams;
  const hotelIdNum = Number(hotelId);
  if (!Number.isInteger(hotelIdNum)) {
    notFound();
  }

  const appUser = await getCurrentAppUser();
  if (!appUser) {
    redirect("/login?error=" + encodeURIComponent("لا يوجد حساب مرتبط بهذا الدخول."));
  }

  const supabase = await createClient();
  const { data: hotel } = await supabase
    .from("hotels")
    .select(
      "id, hotel_name, distance_to_haram_meters, star_rating, address_text, " +
        "check_in_time, check_out_time, is_active, created_at",
    )
    .eq("id", hotelIdNum)
    .maybeSingle<Hotel>();
  if (!hotel) {
    notFound();
  }

  const [{ data: roomTypes }, { data: amenityRows }] = await Promise.all([
    supabase
      .from("room_types")
      .select(
        "id, hotel_id, room_type_name, capacity_adults, size_sqm, " +
          "bed_configuration, created_at",
      )
      .eq("hotel_id", hotelIdNum)
      .order("room_type_name")
      .overrideTypes<RoomType[], { merge: false }>(),
    supabase
      .from("hotel_amenities")
      .select("hotel_id, amenity, created_at")
      .eq("hotel_id", hotelIdNum)
      .overrideTypes<HotelAmenityRow[], { merge: false }>(),
  ]);

  const isAdmin = appUser.app_role === "admin";
  const selectedAmenities: HotelAmenity[] = (amenityRows ?? []).map((row) => row.amenity);
  const missingFields = missingHotelProfileFields(hotel);

  return (
    <AppShell appUser={appUser}>
      <PageHeader
        breadcrumb={{ href: "/hotels", label: "الفنادق" }}
        title={hotel.hotel_name}
        description={hotel.address_text ?? undefined}
      />

      {error && (
        <p role="alert" className={`${ALERT_ERROR} mb-6`}>
          {error}
        </p>
      )}

      {missingFields.length > 0 && (
        <p className={`${ALERT_STATUS} mb-6`}>
          ملف الفندق غير مكتمل — ناقص: {missingFields.join("، ")}. الوكيل يحتاج هذي
          البيانات ليجاوب على أسئلة العميل عن الفندق.
        </p>
      )}

      {!hotel.is_active && (
        <p className={`${ALERT_STATUS} mb-6`}>هذا الفندق موقوف — لن يُعرض على العملاء.</p>
      )}

      <HotelDetailsForm
        hotel={hotel}
        selectedAmenities={selectedAmenities}
        canEdit={isAdmin}
      />

      <h2 className={`${SECTION_TITLE} mt-10`}>أنواع الغرف</h2>
      <ul className="mt-4 space-y-3">
        {(roomTypes ?? []).map((roomType) => (
          <li key={roomType.id}>
            <RoomTypeCard hotelId={hotel.id} roomType={roomType} canEdit={isAdmin} />
          </li>
        ))}
      </ul>
      {(roomTypes ?? []).length === 0 && (
        <p className={`${ALERT_STATUS} mt-4`}>لا توجد أنواع غرف لهذا الفندق بعد.</p>
      )}

      {isAdmin && <AddRoomTypeForm hotelId={hotel.id} />}
    </AppShell>
  );
}

function AddRoomTypeForm({ hotelId }: { hotelId: number }) {
  const createThisRoomType = createRoomType.bind(null, hotelId);
  return (
    <div className={`${CARD} mt-8`}>
      <h3 className={SECTION_TITLE}>إضافة نوع غرفة</h3>
      <form action={createThisRoomType} className="mt-4 flex flex-wrap items-end gap-3">
        <div className="min-w-48 flex-1">
          <label htmlFor="new-room-type-name" className={LABEL}>
            الاسم
          </label>
          <input
            id="new-room-type-name"
            name="room_type_name"
            required
            className={`${INPUT} mt-1 w-full`}
          />
        </div>
        <button type="submit" className={BUTTON_PRIMARY}>
          إضافة
        </button>
      </form>
    </div>
  );
}
