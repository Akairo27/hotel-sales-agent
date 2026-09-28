// Client-side pre-check for admin_set_allotments
// (db/migrations/0029_allotment_entry.sql). Same split as
// admin/lib/priceOverrideRange.ts: a live, per-keystroke night count in the
// form as the primary guard, with the RPC's own RAISE EXCEPTION checks as
// the server-side backstop.
//
// 366, not 180 (price_overrides' cap): matches admin_set_allotments' own
// check exactly (see that migration's comment) — a hotel entering a full
// calendar year at once, including a leap day, is the expected common case
// here, unlike a short-lived price override.
export const MAX_ALLOTMENT_ENTRY_RANGE_NIGHTS = 366;

export interface AllotmentEntryRangeValidationResult {
  valid: boolean;
  message: string;
}

const VALID: AllotmentEntryRangeValidationResult = { valid: true, message: "" };

function invalid(message: string): AllotmentEntryRangeValidationResult {
  return { valid: false, message };
}

// firstNight/lastNight are "YYYY-MM-DD" <input type="date"> values, both
// inclusive — parsed as UTC midnight so the count can't be off by one
// across a local timezone's DST transition, same reasoning as
// priceOverrideRange.ts's nightCount.
export function nightCount(firstNight: string, lastNight: string): number {
  const firstMs = Date.parse(`${firstNight}T00:00:00Z`);
  const lastMs = Date.parse(`${lastNight}T00:00:00Z`);
  return Math.round((lastMs - firstMs) / 86_400_000) + 1;
}

export function validateAllotmentEntryRange(
  firstNight: string,
  lastNight: string,
  totalRooms: number,
  costPerNightRiyals: number
): AllotmentEntryRangeValidationResult {
  if (!firstNight || !lastNight) {
    return invalid("يجب تحديد أول ليلة وآخر ليلة.");
  }
  if (lastNight < firstNight) {
    return invalid("آخر ليلة يجب ألا تسبق أول ليلة.");
  }
  const nights = nightCount(firstNight, lastNight);
  if (nights > MAX_ALLOTMENT_ENTRY_RANGE_NIGHTS) {
    return invalid(
      `هذا المدى يغطي ${nights} ليلة — الحد الأقصى ${MAX_ALLOTMENT_ENTRY_RANGE_NIGHTS} ليلة ` +
        "لكل حفظ. من يحتاج مدى أطول يدخله على دفعتين."
    );
  }
  if (!Number.isInteger(totalRooms) || totalRooms < 0) {
    return invalid("عدد الغرف يجب أن يكون رقماً صحيحاً غير سالب.");
  }
  if (!Number.isInteger(costPerNightRiyals) || costPerNightRiyals < 0) {
    return invalid("التكلفة لليلة يجب أن تكون رقماً صحيحاً غير سالب (بالريال).");
  }
  return VALID;
}
