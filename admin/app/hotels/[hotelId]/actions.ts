"use server";

import { redirect } from "next/navigation";
import { createClient } from "@/utils/supabase/server";
import { getCurrentAppUser } from "@/lib/session";
import { translateConstraintError } from "@/lib/postgresErrors";
import {
  type HotelAmenity,
  parseHotelDetails,
  parseRoomTypeDetails,
} from "@/lib/hotelDetails";

function hotelPath(hotelId: number): string {
  return `/hotels/${hotelId}`;
}

function redirectWithError(hotelId: number, message: string): never {
  redirect(hotelPath(hotelId) + "?error=" + encodeURIComponent(message));
}

// See admin/app/hotels/actions.ts's requireAdmin for why a Server Action
// re-checks the role itself instead of trusting that only an admin ever
// saw the form that submits to it.
async function requireAdmin(hotelId: number): Promise<void> {
  const appUser = await getCurrentAppUser();
  if (!appUser || appUser.app_role !== "admin") {
    redirectWithError(hotelId, "هذا الإجراء يتطلب صلاحية المدير.");
  }
}

function readRoomTypeName(hotelId: number, formData: FormData): string {
  const roomTypeName = formData.get("room_type_name");
  if (typeof roomTypeName !== "string" || roomTypeName.trim() === "") {
    redirectWithError(hotelId, "اسم نوع الغرفة مطلوب.");
  }
  return roomTypeName.trim();
}

export async function createRoomType(hotelId: number, formData: FormData): Promise<void> {
  await requireAdmin(hotelId);
  const roomTypeName = readRoomTypeName(hotelId, formData);

  const supabase = await createClient();
  const { error } = await supabase
    .from("room_types")
    .insert({ hotel_id: hotelId, room_type_name: roomTypeName });
  if (error) {
    redirectWithError(hotelId, error.message);
  }
  redirect(hotelPath(hotelId));
}

export async function renameRoomType(
  hotelId: number,
  roomTypeId: number,
  formData: FormData,
): Promise<void> {
  await requireAdmin(hotelId);
  const roomTypeName = readRoomTypeName(hotelId, formData);

  const supabase = await createClient();
  // Same zero-rows-not-an-error RLS behavior as hotels/actions.ts's
  // renameHotel — see the comment there.
  const { data, error } = await supabase
    .from("room_types")
    .update({ room_type_name: roomTypeName })
    .eq("id", roomTypeId)
    .select("id");
  if (error) {
    redirectWithError(hotelId, error.message);
  }
  if (!data || data.length === 0) {
    redirectWithError(hotelId, "غير مصرح لك بتعديل نوع الغرفة هذا.");
  }
  redirect(hotelPath(hotelId));
}

/** Replaces a hotel's amenity rows with exactly `amenities`.
 *
 * Amenities are add/remove only — migration 0023 grants authenticated no
 * UPDATE on hotel_amenities, because (hotel_id, amenity) is the whole row.
 * A checkbox group submits the complete intended set, so this clears the
 * hotel's rows and re-inserts the selection rather than diffing the two:
 * two statements whose effect is readable at a glance, against a filter
 * built by string-concatenating amenity values into a PostgREST `not.in`
 * list. The only cost is that created_at is reset on an amenity that was
 * already there, and nothing reads that column — it is not audited and
 * appears nowhere in ARCHITECTURE.md.
 *
 * Throws nothing: returns the first error message, or null on success.
 */
async function replaceAmenities(
  hotelId: number,
  amenities: HotelAmenity[],
): Promise<string | null> {
  const supabase = await createClient();

  const { error: deleteError } = await supabase
    .from("hotel_amenities")
    .delete()
    .eq("hotel_id", hotelId);
  if (deleteError) {
    return translateConstraintError(deleteError.message);
  }

  if (amenities.length === 0) {
    return null;
  }

  const { error: insertError } = await supabase
    .from("hotel_amenities")
    .insert(amenities.map((amenity) => ({ hotel_id: hotelId, amenity })));
  if (insertError) {
    return translateConstraintError(insertError.message);
  }
  return null;
}

export async function updateHotelDetails(
  hotelId: number,
  formData: FormData,
): Promise<void> {
  await requireAdmin(hotelId);

  const parsed = parseHotelDetails(formData);
  if (!parsed.valid) {
    redirectWithError(hotelId, parsed.message);
  }

  const supabase = await createClient();
  const { data, error } = await supabase
    .from("hotels")
    .update(parsed.value.patch)
    .eq("id", hotelId)
    .select("id");
  if (error) {
    redirectWithError(hotelId, translateConstraintError(error.message));
  }
  if (!data || data.length === 0) {
    redirectWithError(hotelId, "غير مصرح لك بتعديل هذا الفندق.");
  }

  // Deliberately after the hotels UPDATE, not before: the update is the
  // statement RLS rejects for a non-admin, so running it first means an
  // unauthorized caller never reaches the amenity writes at all.
  const amenityError = await replaceAmenities(hotelId, parsed.value.amenities);
  if (amenityError !== null) {
    redirectWithError(hotelId, amenityError);
  }

  redirect(hotelPath(hotelId));
}

export async function updateRoomTypeDetails(
  hotelId: number,
  roomTypeId: number,
  formData: FormData,
): Promise<void> {
  await requireAdmin(hotelId);

  const parsed = parseRoomTypeDetails(formData);
  if (!parsed.valid) {
    redirectWithError(hotelId, parsed.message);
  }

  const supabase = await createClient();
  const { data, error } = await supabase
    .from("room_types")
    .update(parsed.value)
    .eq("id", roomTypeId)
    .select("id");
  if (error) {
    redirectWithError(hotelId, translateConstraintError(error.message));
  }
  if (!data || data.length === 0) {
    redirectWithError(hotelId, "غير مصرح لك بتعديل نوع الغرفة هذا.");
  }
  redirect(hotelPath(hotelId));
}
