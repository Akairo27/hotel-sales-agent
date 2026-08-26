import Link from "next/link";
import { redirect } from "next/navigation";
import { createClient } from "@/utils/supabase/server";
import { getCurrentAppUser } from "@/lib/session";
import type { Hotel } from "@/lib/types";
import { ALERT_ERROR, BUTTON_PRIMARY, BUTTON_SECONDARY, CARD, INPUT, LABEL, PAGE, SECTION_TITLE } from "@/lib/ui";
import { BackLink } from "@/app/_components/BackLink";
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
    .select("id, hotel_name, created_at")
    .order("hotel_name")
    .overrideTypes<Hotel[], { merge: false }>();

  const isAdmin = appUser.app_role === "admin";

  return (
    <main className={PAGE}>
      <BackLink href="/dashboard">لوحة التحكم</BackLink>
      <h1 className="text-2xl font-semibold text-foreground sm:text-3xl">الفنادق</h1>

      {error && (
        <p role="alert" className={`${ALERT_ERROR} mt-4`}>
          {error}
        </p>
      )}

      <ul className="mt-8 space-y-3">
        {(hotels ?? []).map((hotel) => (
          <li key={hotel.id} className={CARD}>
            <div className="flex flex-wrap items-center justify-between gap-4">
              <Link
                href={`/hotels/${hotel.id}`}
                className="text-base font-medium text-foreground transition hover:text-accent"
              >
                {hotel.hotel_name}
              </Link>
              {isAdmin && <RenameHotelForm hotel={hotel} />}
            </div>
          </li>
        ))}
      </ul>

      {isAdmin && <AddHotelForm />}
    </main>
  );
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
