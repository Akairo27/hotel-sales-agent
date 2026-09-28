"use server";

import { redirect } from "next/navigation";
import { revalidatePath } from "next/cache";
import { createClient } from "@/utils/supabase/server";
import { getCurrentAppUser } from "@/lib/session";
import { validateAllotmentEntryRange } from "@/lib/allotmentEntryRange";
import { halalasToRiyals, riyalsToHalalas } from "@/lib/money";
import type { AdminSetAllotmentsRow, AllotmentEntryAction } from "@/lib/types";

const ALLOTMENTS_PATH = "/allotments";

function redirectWithError(message: string): never {
  redirect(ALLOTMENTS_PATH + "?error=" + encodeURIComponent(message));
}

// Defense-in-depth fast path in front of migration 0029's RLS policies
// (current_app_role() = 'admin' AND current_user_can_view_cost(), enforced
// inside admin_set_allotments itself), which remain the real enforcement
// layer — same pattern as every other write in this dashboard.
async function requireAdminWithCostVisibility(): Promise<string | null> {
  const appUser = await getCurrentAppUser();
  if (!appUser || appUser.app_role !== "admin" || !appUser.can_view_cost) {
    return "هذا الإجراء يتطلب صلاحية المدير وصلاحية عرض التكلفة معاً.";
  }
  return null;
}

// The screen's dates are first-night/last-night, both inclusive (plan
// section 4); admin_set_allotments takes an end-exclusive range. Computed
// in UTC on the "YYYY-MM-DD" string, same reasoning as
// admin/lib/allotmentEntryRange.ts's own nightCount.
function toExclusiveEndDate(lastNightInclusive: string): string {
  const ms = Date.parse(`${lastNightInclusive}T00:00:00Z`);
  return new Date(ms + 86_400_000).toISOString().slice(0, 10);
}

interface CallAdminSetAllotmentsParams {
  hotelId: number;
  roomTypeId: number;
  firstNight: string;
  lastNightInclusive: string;
  totalRooms: number;
  costPerNightHalalas: number;
  dryRun: boolean;
}

async function callAdminSetAllotments(
  params: CallAdminSetAllotmentsParams
): Promise<{ rows: AdminSetAllotmentsRow[]; error: string | null }> {
  const supabase = await createClient();
  const { data, error } = await supabase.rpc("admin_set_allotments", {
    p_hotel_id: params.hotelId,
    p_room_type_id: params.roomTypeId,
    p_from_date: params.firstNight,
    p_to_date_exclusive: toExclusiveEndDate(params.lastNightInclusive),
    p_total_rooms: params.totalRooms,
    p_cost_per_night: params.costPerNightHalalas,
    p_dry_run: params.dryRun,
  });
  // Not .overrideTypes(): this client has no Database generic (no rpc call
  // in this codebase types one), so .rpc()'s inferred Result is a naked
  // `any` — postgrest-js's array/single-object mismatch check distributes
  // over that `any` and always includes its "single cast to array" error
  // branch, so .overrideTypes<T[]>() can never type-check here regardless
  // of the real (correctly array-shaped, RETURNS TABLE) response. A direct
  // cast is the honest boundary instead.
  const rows = data as unknown as AdminSetAllotmentsRow[] | null;
  return { rows: rows ?? [], error: error?.message ?? null };
}

export interface AllotmentEntryPreviewNight {
  stayDate: string;
  action: AllotmentEntryAction;
  totalRooms: number;
  costPerNightRiyals: number;
  reserved: number;
  held: number;
}

function toPreviewNights(rows: AdminSetAllotmentsRow[]): AllotmentEntryPreviewNight[] {
  return rows.map((row) => ({
    stayDate: row.out_stay_date,
    action: row.out_action,
    totalRooms: row.out_total_rooms,
    costPerNightRiyals: halalasToRiyals(row.out_cost_per_night),
    reserved: row.out_reserved,
    held: row.out_held,
  }));
}

export interface AllotmentEntryPreviewResult {
  error?: string;
  nights?: AllotmentEntryPreviewNight[];
}

// Runs the real function with dry_run — every write happens and every
// constraint (including inventory_never_oversold) actually runs, then
// admin_set_allotments unwinds it before returning, so this can never
// report a plan the database would go on to reject.
export async function previewAllotmentEntry(
  hotelId: number,
  roomTypeId: number,
  firstNight: string,
  lastNightInclusive: string,
  totalRooms: number,
  costPerNightRiyals: number
): Promise<AllotmentEntryPreviewResult> {
  const authError = await requireAdminWithCostVisibility();
  if (authError) {
    return { error: authError };
  }
  const validation = validateAllotmentEntryRange(
    firstNight,
    lastNightInclusive,
    totalRooms,
    costPerNightRiyals
  );
  if (!validation.valid) {
    return { error: validation.message };
  }

  const { rows, error } = await callAdminSetAllotments({
    hotelId,
    roomTypeId,
    firstNight,
    lastNightInclusive,
    totalRooms,
    costPerNightHalalas: riyalsToHalalas(costPerNightRiyals),
    dryRun: true,
  });
  if (error) {
    return { error };
  }
  return { nights: toPreviewNights(rows) };
}

export interface AllotmentEntryResult {
  error?: string;
}

// The confirm step: identical inputs to the preview that produced them,
// resubmitted for real. Not fed the previewed rows directly — a night's
// reserved/held count can change between preview and confirm (a customer
// booking the same night in between), so this re-runs the same checks
// admin_set_allotments always runs, rather than trusting a stale preview.
export async function submitAllotmentEntry(
  hotelId: number,
  roomTypeId: number,
  firstNight: string,
  lastNightInclusive: string,
  totalRooms: number,
  costPerNightRiyals: number
): Promise<AllotmentEntryResult> {
  const authError = await requireAdminWithCostVisibility();
  if (authError) {
    return { error: authError };
  }
  const validation = validateAllotmentEntryRange(
    firstNight,
    lastNightInclusive,
    totalRooms,
    costPerNightRiyals
  );
  if (!validation.valid) {
    return { error: validation.message };
  }

  const { error } = await callAdminSetAllotments({
    hotelId,
    roomTypeId,
    firstNight,
    lastNightInclusive,
    totalRooms,
    costPerNightHalalas: riyalsToHalalas(costPerNightRiyals),
    dryRun: false,
  });
  if (error) {
    return { error };
  }
  revalidatePath(ALLOTMENTS_PATH);
  return {};
}

// The existing table's inline per-row edit, now going through
// admin_set_allotments (the only writer of allotments/room_night_inventory
// since migration 0029) instead of the retired-in-practice
// admin_set_allotment_cost, and now covering total_rooms alongside cost —
// a single-night call (first night === last night).
export async function updateAllotmentNight(
  hotelId: number,
  roomTypeId: number,
  stayDate: string,
  formData: FormData
): Promise<void> {
  const authError = await requireAdminWithCostVisibility();
  if (authError) {
    redirectWithError(authError);
  }

  const totalRooms = Number(formData.get("total_rooms"));
  const costPerNightRiyals = Number(formData.get("cost_per_night_riyals"));
  const validation = validateAllotmentEntryRange(stayDate, stayDate, totalRooms, costPerNightRiyals);
  if (!validation.valid) {
    redirectWithError(validation.message);
  }

  const { error } = await callAdminSetAllotments({
    hotelId,
    roomTypeId,
    firstNight: stayDate,
    lastNightInclusive: stayDate,
    totalRooms,
    costPerNightHalalas: riyalsToHalalas(costPerNightRiyals),
    dryRun: false,
  });
  if (error) {
    redirectWithError(error);
  }
  redirect(ALLOTMENTS_PATH);
}
