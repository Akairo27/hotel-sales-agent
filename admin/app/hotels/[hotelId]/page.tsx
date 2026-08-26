import { notFound, redirect } from "next/navigation";
import { createClient } from "@/utils/supabase/server";
import { getCurrentAppUser } from "@/lib/session";
import type { Hotel, RoomType } from "@/lib/types";
import { ALERT_ERROR, BUTTON_PRIMARY, BUTTON_SECONDARY, CARD, INPUT, LABEL, SECTION_TITLE } from "@/lib/ui";
import { AppShell } from "@/app/_components/AppShell";
import { PageHeader } from "@/app/_components/PageHeader";
import { createRoomType, renameRoomType } from "./actions";

export default async function HotelRoomTypesPage({
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
    .select("id, hotel_name, created_at")
    .eq("id", hotelIdNum)
    .maybeSingle<Hotel>();
  if (!hotel) {
    notFound();
  }

  const { data: roomTypes } = await supabase
    .from("room_types")
    .select("id, hotel_id, room_type_name, created_at")
    .eq("hotel_id", hotelIdNum)
    .order("room_type_name")
    .overrideTypes<RoomType[], { merge: false }>();

  const isAdmin = appUser.app_role === "admin";

  return (
    <AppShell appUser={appUser}>
      <PageHeader
        breadcrumb={{ href: "/hotels", label: "الفنادق" }}
        title={hotel.hotel_name}
      />

      {error && (
        <p role="alert" className={`${ALERT_ERROR} mb-6`}>
          {error}
        </p>
      )}

      <h2 className={SECTION_TITLE}>أنواع الغرف</h2>
      <ul className="mt-4 space-y-3">
        {(roomTypes ?? []).map((roomType) => (
          <li key={roomType.id} className={CARD}>
            <div className="flex flex-wrap items-center justify-between gap-4">
              <span className="text-base font-medium text-foreground">
                {roomType.room_type_name}
              </span>
              {isAdmin && <RenameRoomTypeForm hotelId={hotel.id} roomType={roomType} />}
            </div>
          </li>
        ))}
      </ul>

      {isAdmin && <AddRoomTypeForm hotelId={hotel.id} />}
    </AppShell>
  );
}

function RenameRoomTypeForm({ hotelId, roomType }: { hotelId: number; roomType: RoomType }) {
  const renameThisRoomType = renameRoomType.bind(null, hotelId, roomType.id);
  return (
    <form action={renameThisRoomType} className="flex items-end gap-2">
      <div className="w-44">
        <label
          htmlFor={`room-type-name-${roomType.id}`}
          className="mb-1 block text-xs text-muted-foreground"
        >
          الاسم الجديد
        </label>
        <input
          id={`room-type-name-${roomType.id}`}
          name="room_type_name"
          defaultValue={roomType.room_type_name}
          required
          className={`${INPUT} w-full`}
        />
      </div>
      <button type="submit" className={BUTTON_SECONDARY}>
        حفظ
      </button>
    </form>
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
