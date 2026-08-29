// Layer 2 of the price_rules/price_overrides error-translation design: a
// safety net for whatever CHECK/UNIQUE violation still reaches Postgres
// despite admin/lib/priceRuleBands.ts's client-side validation (a
// concurrent write, a bug in that port, or a constraint like
// price_rules_global_is_complete / price_rules_single_global that depends
// on the whole table's state rather than the submitted shape alone, and so
// has no client-side equivalent to check ahead of time).
//
// An explicit constraint-name-to-message map, deliberately not a generic
// strip/clean of Postgres's own message: that would be fragile against
// wording changes and could still leak internal identifiers (constraint
// names, column names) the raw string carries. Every entry here is a
// specific, known constraint from db/migrations/0006_price_rules.sql,
// 0007_price_overrides.sql, and 0020_price_rules_disable_flag.sql.
//
// PostgREST's error response has no separate constraint-name field to key
// on directly — only a message that embeds it, in a stable, well-known
// shape: `violates check constraint "name"` or
// `violates unique constraint "name"`. Extracted here once, matched
// against the map below.
const CONSTRAINT_NAME_PATTERN = /violates (?:check|unique) constraint "([a-z0-9_]+)"/;

const CONSTRAINT_MESSAGES: Record<string, string> = {
  price_rules_scope_valid: "نطاق القاعدة غير صالح.",
  price_rules_scope_id_matches_scope:
    "القاعدة العامة لا تأخذ رقم مرجع، وأي قاعدة أخرى تحتاج رقم مرجع صالح.",
  price_rules_global_is_complete:
    "القاعدة العامة هي أساس سلسلة التوريث ولا يوجد ما تسقط إليه — يجب تحديد الهامش وحد الربح الأدنى ومنحنى الطلب معاً.",
  price_rules_min_profit_bands_valid:
    "فترات حد الربح الأدنى غير مكتملة أو متداخلة أو فيها فجوة — راجع الفترات وحاول مرة أخرى.",
  price_rules_demand_curve_lead_time_bands_valid:
    "فترات مدة الحجز في منحنى الطلب غير مكتملة أو متداخلة أو فيها فجوة.",
  price_rules_demand_curve_occupancy_bands_valid:
    "فترات نسبة الإشغال في منحنى الطلب غير مكتملة أو متداخلة أو فيها فجوة.",
  price_rules_target_margin_bps_non_negative: "الهامش المستهدف لا يمكن أن يكون سالباً.",
  price_rules_single_global: "توجد قاعدة عامة واحدة بالفعل — لا يمكن إنشاء أخرى.",
  price_rules_global_always_active:
    "لا يمكن تعطيل القاعدة العامة — هي أساس سلسلة التوريث ولا شيء يسقط إليه بدونها.",
  price_overrides_ask_price_non_negative: "سعر العرض لا يمكن أن يكون سالباً.",
  price_overrides_min_allowed_non_negative: "الحد الأدنى المسموح لا يمكن أن يكون سالباً.",
  price_overrides_min_allowed_not_above_ask:
    "الحد الأدنى المسموح لا يمكن أن يتجاوز سعر العرض.",
  // db/migrations/0023_hotel_details.sql. admin/lib/hotelDetails.ts checks
  // each of these in the form first; these entries are the backstop for
  // whatever reaches Postgres anyway — a concurrent write, or a payload
  // that did not come from that form.
  hotels_distance_to_haram_positive: "المسافة عن الحرم يجب أن تكون أكبر من صفر.",
  hotels_star_rating_valid: "التصنيف يجب أن يكون بين نجمة وخمس نجوم.",
  hotels_address_text_not_blank: "العنوان لا يمكن أن يكون فارغاً — احذفه أو اكتبه كاملاً.",
  room_types_capacity_adults_valid: "عدد الأشخاص يجب أن يكون بين ١ و٢٠.",
  room_types_size_sqm_positive: "المساحة يجب أن تكون أكبر من صفر.",
  room_types_bed_configuration_valid: "توزيع الأسرّة غير معروف.",
  hotel_amenities_known_amenity: "أحد المرافق المختارة غير معروف.",
};

const GENERIC_FALLBACK_MESSAGE = "تعذر الحفظ — تحقق من صحة القيم المدخلة.";

// Every actions.ts under admin/app/price-rules/ and admin/app/price-overrides/
// should route a Supabase error through this before it ever reaches
// redirectWithError — never the raw error.message, per the incident this
// whole layer exists to prevent (a raw English Postgres string like
// `new row for relation "price_rules" violates check constraint
// "price_rules_min_profit_bands_valid"` rendered directly in the admin
// dashboard's Arabic UI). Coverage grew with the screens that needed it:
// price_rules and price_overrides first, then hotels/room_types/
// hotel_amenities when migration 0023 gave those tables constraints an
// admin can trip. seasons, allotments and users still surface raw errors —
// they have not been through this treatment yet.
// Falls back to the generic message for anything that isn't a recognized
// CHECK/UNIQUE violation too — not just an unrecognized one. A wrapper
// function's own permission error (e.g. admin_upsert_price_rule's "not
// permitted to update this price rule") is still raw English technical
// text; the pre-flight role/can_view_cost check in the calling Server
// Action is what's meant to catch that case before the RPC ever runs, so
// reaching this function at all means something unexpected happened, and
// showing a vague-but-Arabic message beats showing an accurate-but-raw one.
export function translateConstraintError(message: string): string {
  const match = CONSTRAINT_NAME_PATTERN.exec(message);
  if (match === null) {
    return GENERIC_FALLBACK_MESSAGE;
  }
  const constraintName = match[1];
  return CONSTRAINT_MESSAGES[constraintName] ?? GENERIC_FALLBACK_MESSAGE;
}
