import Link from "next/link";
import { redirect } from "next/navigation";
import { createClient } from "@/utils/supabase/server";
import { getCurrentAppUser } from "@/lib/session";
import type { Hotel } from "@/lib/types";
import { missingHotelProfileFields } from "@/lib/hotelDetails";
import { ALERT_ERROR, BUTTON_PRIMARY, BUTTON_SECONDARY, CARD, INPUT, LABEL, SECTION_TITLE } from "@/lib/ui";
import { AppShell } from "@/app/_components/AppShell";
import { PageHeader } from "@/app/_components/PageHeader";
import { createHotel, renameHotel } from "./actions";

export default async function HotelsPage({
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
  const { data: hotels } = await supabase
    .from("hotels")
    .select(
      "id, hotel_name, distance_to_haram_meters, star_rating, address_text, " +
        "check_in_time, check_out_time, is_active, created_at",
    )
    .order("hotel_name")
    .overrideTypes<Hotel[], { merge: false }>();

  const isAdmin = appUser.app_role === "admin";

  return (
    <AppShell appUser={appUser}>
      <PageHeader title="الفنادق" description="افتح فندقاً لإدارة أنواع غرفه." />

      {error && (
        <p role="alert" className={`${ALERT_ERROR} mb-6`}>
          {error}
        </p>
      )}

      <ul className="space-y-3">
        {(hotels ?? []).map((hotel) => (
          <li key={hotel.id} className={CARD}>
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div>
                <Link
                  href={`/hotels/${hotel.id}`}
                  className="text-base font-medium text-foreground transition hover:text-accent"
                >
                  {hotel.hotel_name}
                </Link>
                <span className="mt-1 block text-sm text-muted-foreground">
                  {summarize(hotel)}
                </span>
              </div>
              {isAdmin && <RenameHotelForm hotel={hotel} />}
            </div>
          </li>
        ))}
      </ul>

      {isAdmin && <AddHotelForm />}
    </AppShell>
  );
}

// The two facts that decide whether a hotel is worth opening — how far it
// is from the Haram and how it is rated — plus an explicit flag when the
// profile is too incomplete for the agent to describe it at all.
function summarize(hotel: Hotel): string {
  const missing = missingHotelProfileFields(hotel);
  if (missing.length > 0) {
    return `ملف غير مكتمل — ناقص: ${missing.join("، ")}`;
  }
  const distance = `${hotel.distance_to_haram_meters} متر عن الحرم`;
  const stars = `${hotel.star_rating} نجوم`;
  return hotel.is_active ? `${distance} · ${stars}` : `${distance} · ${stars} · موقوف`;
}

function RenameHotelForm({ hotel }: { hotel: Hotel }) {
  const renameThisHotel = renameHotel.bind(null, hotel.id);
  return (
    <form action={renameThisHotel} className="flex items-end gap-2">
      <div className="w-44">
        <label htmlFor={`hotel-name-${hotel.id}`} className="mb-1 block text-xs text-muted-foreground">
          الاسم الجديد
        </label>
        <input
          id={`hotel-name-${hotel.id}`}
          name="hotel_name"
          defaultValue={hotel.hotel_name}
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

function AddHotelForm() {
  return (
    <div className={`${CARD} mt-8`}>
      <h2 className={SECTION_TITLE}>إضافة فندق</h2>
      <form action={createHotel} className="mt-4 flex flex-wrap items-end gap-3">
        <div className="min-w-48 flex-1">
          <label htmlFor="new-hotel-name" className={LABEL}>
            اسم الفندق
          </label>
          <input id="new-hotel-name" name="hotel_name" required className={`${INPUT} mt-1 w-full`} />
        </div>
        <button type="submit" className={BUTTON_PRIMARY}>
          إضافة
        </button>
      </form>
    </div>
  );
}
