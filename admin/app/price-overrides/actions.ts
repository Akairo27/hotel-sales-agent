"use server";

import { revalidatePath } from "next/cache";
import { createClient } from "@/utils/supabase/server";
import { getCurrentAppUser } from "@/lib/session";
import { translateConstraintError } from "@/lib/postgresErrors";
import { validateOverrideRange } from "@/lib/priceOverrideRange";

const PRICE_OVERRIDES_PATH = "/price-overrides";

export interface PriceOverrideActionResult {
  error?: string;
  // Informational only, never blocks the save — the client accepts selling
  // below cost close to arrival and leaves that call to sales. Present
  // only when the acting user has can_view_cost (see warnIfBelowCost).
  warning?: string;
}

// Defense-in-depth fast path in front of migration 0021's RLS policies,
// which remain the real enforcement layer — same pattern as every other
// write in this dashboard. Admin-only for now, by explicit decision; the
// extension point for a future sales write path is this one check.
async function requireAdmin(): Promise<string | null> {
  const appUser = await getCurrentAppUser();
  if (!appUser || appUser.app_role !== "admin") {
    return "هذا الإجراء يتطلب صلاحية المدير.";
  }
  return null;
}

// Computed here, not in the RPC, using the existing allotments_for_dashboard
// masking view (migration 0016) — cost_per_night there is already null for
// anyone whose can_view_cost is false, which is exactly what makes this
// warning visible only to can_view_cost holders with no extra logic here.
// Two separate messages, not one combined check: a night where only the
// minimum allowed price is below cost is a different situation (a possible
// loss only if negotiated down) from one where the asking price itself is
// already below cost (a certain loss at the standard offer).
async function warnIfBelowCost(
  hotelId: number,
  roomTypeId: number,
  startDate: string,
  endDate: string,
  askPriceOverride: number,
  minAllowedOverride: number
): Promise<string | undefined> {
  const appUser = await getCurrentAppUser();
  if (!appUser?.can_view_cost) {
    return undefined;
  }
  const supabase = await createClient();
  const { data: nights } = await supabase
    .from("allotments_for_dashboard")
    .select("cost_per_night")
    .eq("hotel_id", hotelId)
    .eq("room_type_id", roomTypeId)
    .gte("stay_date", startDate)
    .lte("stay_date", endDate)
    .not("cost_per_night", "is", null)
    .overrideTypes<{ cost_per_night: number }[], { merge: false }>();

  if (!nights || nights.length === 0) {
    return undefined;
  }
  const belowAsk = nights.filter((n) => n.cost_per_night > askPriceOverride).length;
  const belowMinOnly = nights.filter(
    (n) => n.cost_per_night <= askPriceOverride && n.cost_per_night > minAllowedOverride
  ).length;

  const parts: string[] = [];
  if (belowAsk > 0) {
    parts.push(
      `سعر العرض أقل من التكلفة في ${belowAsk} ليلة — خسارة مؤكدة عند هذا السعر.`
    );
  }
  if (belowMinOnly > 0) {
    parts.push(
      `الحد الأدنى المسموح أقل من التكلفة في ${belowMinOnly} ليلة — يسمح بالتفاوض إلى خسارة.`
    );
  }
  return parts.length > 0 ? parts.join(" ") : undefined;
}

// The only entry point for creating or editing overrides — the admin
// screen always works over a date range (a single night is a range of one),
// per the decision to generate one row per night rather than edit rows
// individually. admin/lib/priceOverrideRange.ts's validation is the primary
// guard (a live night counter in the form); this re-check is the
// server-side backstop, same defense-in-depth split as price_rules' band
// validation.
export async function upsertPriceOverrides(
  hotelId: number,
  roomTypeId: number,
  startDate: string,
  endDate: string,
  askPriceOverride: number,
  minAllowedOverride: number,
  expiresAt: string
): Promise<PriceOverrideActionResult> {
  const authError = await requireAdmin();
  if (authError) {
    return { error: authError };
  }
  const validation = validateOverrideRange(
    startDate,
    endDate,
    askPriceOverride,
    minAllowedOverride
  );
  if (!validation.valid) {
    return { error: validation.message };
  }

  const supabase = await createClient();
  const { error } = await supabase.rpc("admin_upsert_price_overrides", {
    p_hotel_id: hotelId,
    p_room_type_id: roomTypeId,
    p_start_date: startDate,
    p_end_date: endDate,
    p_ask_price_override: askPriceOverride,
    p_min_allowed_override: minAllowedOverride,
    p_expires_at: expiresAt,
  });
  if (error) {
    return { error: translateConstraintError(error.message) };
  }
  revalidatePath(PRICE_OVERRIDES_PATH);

  const warning = await warnIfBelowCost(
    hotelId,
    roomTypeId,
    startDate,
    endDate,
    askPriceOverride,
    minAllowedOverride
  );
  return warning ? { warning } : {};
}

// Deliberately not folded into upsertPriceOverrides, same reasoning as
// price_rules' setPriceRuleActive (migration 0020): ending an override
// early is a toggle on one row, not an edit of its values, and it must not
// re-show the below-cost warning for values that did not change. "Ending"
// is expires_at set to now — there is no separate is_active-style column
// here (see migration 0021's own comment on why one is unnecessary).
// The server computes "now", not the caller: an "end this immediately"
// action means immediately when the server processes it, not whenever the
// browser rendered the button, and a client-supplied timestamp for an
// audited write is not something to trust here.
export async function endPriceOverrideNow(
  hotelId: number,
  roomTypeId: number,
  stayDate: string,
  askPriceOverride: number,
  minAllowedOverride: number
): Promise<PriceOverrideActionResult> {
  const authError = await requireAdmin();
  if (authError) {
    return { error: authError };
  }

  const supabase = await createClient();
  const { error } = await supabase.rpc("admin_upsert_price_overrides", {
    p_hotel_id: hotelId,
    p_room_type_id: roomTypeId,
    p_start_date: stayDate,
    p_end_date: stayDate,
    p_ask_price_override: askPriceOverride,
    p_min_allowed_override: minAllowedOverride,
    p_expires_at: new Date().toISOString(),
  });
  if (error) {
    return { error: translateConstraintError(error.message) };
  }
  revalidatePath(PRICE_OVERRIDES_PATH);
  return {};
}
