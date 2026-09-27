// Hotel and room-type attribute vocabulary and form parsing, mirroring
// db/migrations/0023_hotel_details.sql and 0028_hotel_location.sql. The
// database is the source of truth: every bound here has a CHECK constraint
// behind it, and this module exists so a typo is caught in the form instead
// of coming back as a Postgres error — the same defense-in-depth split as
// admin/lib/priceOverrideRange.ts, not a replacement for the constraint.
//
// HOTEL_AMENITIES, BED_CONFIGURATIONS, HOTEL_CITIES and HOTEL_ZONES are
// asserted against those migrations' own CHECK lists in
// hotelDetails.conformance.test.ts, so the two cannot drift apart silently.

export const HOTEL_AMENITIES = [
  "haram_view",
  "shuttle_to_haram",
  "breakfast_included",
  "restaurant",
  "room_service",
  "prayer_room",
  "wifi",
  "parking",
  "laundry",
  "elevator",
  "wheelchair_accessible",
  "family_rooms",
] as const;

export type HotelAmenity = (typeof HOTEL_AMENITIES)[number];

export const AMENITY_LABELS: Record<HotelAmenity, string> = {
  haram_view: "إطلالة على الحرم",
  shuttle_to_haram: "نقل إلى الحرم",
  breakfast_included: "إفطار مشمول",
  restaurant: "مطعم",
  room_service: "خدمة الغرف",
  prayer_room: "مصلى",
  wifi: "واي فاي",
  parking: "موقف سيارات",
  laundry: "مغسلة",
  elevator: "مصعد",
  wheelchair_accessible: "مهيأ لذوي الإعاقة",
  family_rooms: "غرف عائلية",
};

export const BED_CONFIGURATIONS = [
  "single",
  "double",
  "twin",
  "triple",
  "quad",
] as const;

export type BedConfiguration = (typeof BED_CONFIGURATIONS)[number];

export const BED_CONFIGURATION_LABELS: Record<BedConfiguration, string> = {
  single: "سرير مفرد",
  double: "سرير مزدوج",
  twin: "سريران منفصلان",
  triple: "ثلاثة أسرّة",
  quad: "أربعة أسرّة",
};

// Location vocabulary, mirroring db/migrations/0028_hotel_location.sql. Both
// lists are asserted against that migration's own CHECK constraints in
// hotelDetails.conformance.test.ts.
export const HOTEL_CITIES = ["makkah", "madinah"] as const;

export type HotelCity = (typeof HOTEL_CITIES)[number];

export const CITY_LABELS: Record<HotelCity, string> = {
  makkah: "مكة المكرمة",
  madinah: "المدينة المنورة",
};

// Each zone name starts with its city (hotels_zone_matches_city).
export const HOTEL_ZONES = [
  "makkah_central",
  "makkah_outside",
  "madinah_central",
  "madinah_north",
  "madinah_west",
  "madinah_south",
  "madinah_outside",
] as const;

export type HotelZone = (typeof HOTEL_ZONES)[number];

export const ZONE_LABELS: Record<HotelZone, string> = {
  makkah_central: "المنطقة المركزية (حول الحرم)",
  makkah_outside: "خارج المنطقة المركزية",
  madinah_central: "المنطقة المركزية (حول المسجد النبوي)",
  madinah_north: "المنطقة الشمالية",
  madinah_west: "المنطقة الغربية",
  madinah_south: "المنطقة الجنوبية",
  madinah_outside: "خارج المنطقة المركزية",
};

export function zoneBelongsToCity(zone: HotelZone, city: HotelCity): boolean {
  return zone.startsWith(`${city}_`);
}

// ISO weekdays, as the database stores them: 1 is Monday, 7 is Sunday. The
// default weekend is Friday and Saturday; the form lists the days in the
// order the week is read in Arabic, starting on Saturday.
export const ISO_WEEKDAYS = [1, 2, 3, 4, 5, 6, 7] as const;

export type IsoWeekday = (typeof ISO_WEEKDAYS)[number];

export const WEEKDAY_LABELS: Record<IsoWeekday, string> = {
  1: "الاثنين",
  2: "الثلاثاء",
  3: "الأربعاء",
  4: "الخميس",
  5: "الجمعة",
  6: "السبت",
  7: "الأحد",
};

export const WEEKDAY_DISPLAY_ORDER: readonly IsoWeekday[] = [6, 7, 1, 2, 3, 4, 5];

export const DEFAULT_WEEKEND_DAYS: readonly IsoWeekday[] = [5, 6];

export const MAX_DISTRICT_NAME_LENGTH = 60;

export const MIN_STAR_RATING = 1;
export const MAX_STAR_RATING = 5;
export const MIN_CAPACITY_ADULTS = 1;
export const MAX_CAPACITY_ADULTS = 20;

export type FieldResult<T> =
  | { valid: true; value: T }
  | { valid: false; message: string };

interface IntegerBounds {
  label: string;
  min: number;
  max?: number;
}

// Every detail column is nullable (see the migration's own comment on why
// presence is not enforced in SQL), so an empty field is a legitimate
// "not recorded yet" rather than an error — but a field with something
// unparseable in it never is.
export function parseOptionalInteger(
  raw: FormDataEntryValue | null,
  bounds: IntegerBounds,
): FieldResult<number | null> {
  if (typeof raw !== "string" || raw.trim() === "") {
    return { valid: true, value: null };
  }

  // Number("12abc") is NaN, but Number("") is 0 and Number(" ") is 0 —
  // the empty check above is what keeps a blank field from being stored
  // as a zero that then trips the DB's own > 0 constraint.
  const parsed = Number(raw.trim());
  if (!Number.isInteger(parsed)) {
    return { valid: false, message: `${bounds.label}: أدخل رقماً صحيحاً.` };
  }
  if (parsed < bounds.min || (bounds.max !== undefined && parsed > bounds.max)) {
    const range =
      bounds.max === undefined
        ? `${bounds.min} أو أكثر`
        : `بين ${bounds.min} و${bounds.max}`;
    return { valid: false, message: `${bounds.label}: القيمة يجب أن تكون ${range}.` };
  }
  return { valid: true, value: parsed };
}

export function parseOptionalText(raw: FormDataEntryValue | null): string | null {
  if (typeof raw !== "string" || raw.trim() === "") {
    return null;
  }
  return raw.trim();
}

// <input type="time"> submits "HH:MM" (or "HH:MM:SS" where a step is set);
// anything else means the value did not come from that control.
const TIME_PATTERN = /^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$/;

export function parseOptionalTime(
  raw: FormDataEntryValue | null,
  label: string,
): FieldResult<string | null> {
  const text = parseOptionalText(raw);
  if (text === null) {
    return { valid: true, value: null };
  }
  if (!TIME_PATTERN.test(text)) {
    return { valid: false, message: `${label}: صيغة الوقت غير صالحة.` };
  }
  return { valid: true, value: text };
}

export function parseAmenities(raw: FormDataEntryValue[]): FieldResult<HotelAmenity[]> {
  const known = new Set<string>(HOTEL_AMENITIES);
  const selected: HotelAmenity[] = [];
  for (const entry of raw) {
    if (typeof entry !== "string" || !known.has(entry)) {
      return { valid: false, message: "المرافق: قيمة غير معروفة." };
    }
    // A checkbox group cannot normally submit the same value twice, so a
    // duplicate means the payload was not produced by this form.
    if (selected.includes(entry as HotelAmenity)) {
      return { valid: false, message: "المرافق: قيمة مكررة." };
    }
    selected.push(entry as HotelAmenity);
  }
  return { valid: true, value: selected };
}

export function parseBedConfiguration(
  raw: FormDataEntryValue | null,
): FieldResult<BedConfiguration | null> {
  const text = parseOptionalText(raw);
  if (text === null) {
    return { valid: true, value: null };
  }
  if (!(BED_CONFIGURATIONS as readonly string[]).includes(text)) {
    return { valid: false, message: "توزيع الأسرّة: قيمة غير معروفة." };
  }
  return { valid: true, value: text as BedConfiguration };
}

export function parseCity(raw: FormDataEntryValue | null): FieldResult<HotelCity | null> {
  const text = parseOptionalText(raw);
  if (text === null) {
    return { valid: true, value: null };
  }
  if (!(HOTEL_CITIES as readonly string[]).includes(text)) {
    return { valid: false, message: "المدينة: قيمة غير معروفة." };
  }
  return { valid: true, value: text as HotelCity };
}

// The zone is validated against the city in the same call: the database
// refuses a zone that does not belong to the hotel's city (and a zone with
// no city at all), and saying so here names the field instead of returning
// a constraint error.
export function parseZone(
  raw: FormDataEntryValue | null,
  city: HotelCity | null,
): FieldResult<HotelZone | null> {
  const text = parseOptionalText(raw);
  if (text === null) {
    return { valid: true, value: null };
  }
  if (!(HOTEL_ZONES as readonly string[]).includes(text)) {
    return { valid: false, message: "المنطقة: قيمة غير معروفة." };
  }
  if (city === null) {
    return { valid: false, message: "المنطقة: اختر المدينة أولاً." };
  }
  if (!zoneBelongsToCity(text as HotelZone, city)) {
    return { valid: false, message: "المنطقة: لا تتبع المدينة المختارة." };
  }
  return { valid: true, value: text as HotelZone };
}

// A checkbox group submits the ticked days as separate values. The database
// would accept an empty array (a hotel with no weekend), but this form
// never means that: an empty selection is far more likely to be a slip, so
// it is refused here with a message rather than stored.
export function parseWeekendDays(raw: FormDataEntryValue[]): FieldResult<IsoWeekday[]> {
  const known = new Set<number>(ISO_WEEKDAYS);
  const selected: IsoWeekday[] = [];
  for (const entry of raw) {
    const day = typeof entry === "string" && /^[1-7]$/.test(entry) ? Number(entry) : NaN;
    if (!known.has(day)) {
      return { valid: false, message: "أيام العطلة الأسبوعية: قيمة غير معروفة." };
    }
    if (selected.includes(day as IsoWeekday)) {
      return { valid: false, message: "أيام العطلة الأسبوعية: قيمة مكررة." };
    }
    selected.push(day as IsoWeekday);
  }
  if (selected.length === 0) {
    return { valid: false, message: "أيام العطلة الأسبوعية: اختر يوماً واحداً على الأقل." };
  }
  return { valid: true, value: selected.sort((a, b) => a - b) };
}

// Postgres counts characters (code points), not UTF-16 units, so the length
// is counted the same way: an emoji is one character to both.
export function parseDistrictName(
  raw: FormDataEntryValue | null,
): FieldResult<string | null> {
  const text = parseOptionalText(raw);
  if (text === null) {
    return { valid: true, value: null };
  }
  if (Array.from(text).length > MAX_DISTRICT_NAME_LENGTH) {
    return {
      valid: false,
      message: `اسم الحي: الحد الأقصى ${MAX_DISTRICT_NAME_LENGTH} حرفاً.`,
    };
  }
  return { valid: true, value: text };
}

export interface HotelDetailsPatch {
  distance_to_haram_meters: number | null;
  star_rating: number | null;
  address_text: string | null;
  check_in_time: string | null;
  check_out_time: string | null;
  city: HotelCity | null;
  zone: HotelZone | null;
  district_name: string | null;
  weekend_days: IsoWeekday[];
  is_active: boolean;
}

export function parseHotelDetails(
  formData: FormData,
): FieldResult<{ patch: HotelDetailsPatch; amenities: HotelAmenity[] }> {
  const distance = parseOptionalInteger(formData.get("distance_to_haram_meters"), {
    label: "المسافة عن الحرم",
    min: 1,
  });
  if (!distance.valid) {
    return distance;
  }

  const stars = parseOptionalInteger(formData.get("star_rating"), {
    label: "التصنيف",
    min: MIN_STAR_RATING,
    max: MAX_STAR_RATING,
  });
  if (!stars.valid) {
    return stars;
  }

  const checkIn = parseOptionalTime(formData.get("check_in_time"), "وقت الدخول");
  if (!checkIn.valid) {
    return checkIn;
  }

  const checkOut = parseOptionalTime(formData.get("check_out_time"), "وقت الخروج");
  if (!checkOut.valid) {
    return checkOut;
  }

  const city = parseCity(formData.get("city"));
  if (!city.valid) {
    return city;
  }

  const zone = parseZone(formData.get("zone"), city.value);
  if (!zone.valid) {
    return zone;
  }

  const district = parseDistrictName(formData.get("district_name"));
  if (!district.valid) {
    return district;
  }

  const weekend = parseWeekendDays(formData.getAll("weekend_days"));
  if (!weekend.valid) {
    return weekend;
  }

  const amenities = parseAmenities(formData.getAll("amenities"));
  if (!amenities.valid) {
    return amenities;
  }

  return {
    valid: true,
    value: {
      patch: {
        distance_to_haram_meters: distance.value,
        star_rating: stars.value,
        address_text: parseOptionalText(formData.get("address_text")),
        check_in_time: checkIn.value,
        check_out_time: checkOut.value,
        city: city.value,
        zone: zone.value,
        district_name: district.value,
        weekend_days: weekend.value,
        // An unchecked checkbox submits nothing at all, so absence is
        // false — never "leave the current value alone".
        is_active: formData.get("is_active") === "on",
      },
      amenities: amenities.value,
    },
  };
}

export interface RoomTypeDetailsPatch {
  capacity_adults: number | null;
  size_sqm: number | null;
  bed_configuration: BedConfiguration | null;
}

export function parseRoomTypeDetails(
  formData: FormData,
): FieldResult<RoomTypeDetailsPatch> {
  const capacity = parseOptionalInteger(formData.get("capacity_adults"), {
    label: "عدد الأشخاص",
    min: MIN_CAPACITY_ADULTS,
    max: MAX_CAPACITY_ADULTS,
  });
  if (!capacity.valid) {
    return capacity;
  }

  const size = parseOptionalInteger(formData.get("size_sqm"), {
    label: "المساحة",
    min: 1,
  });
  if (!size.valid) {
    return size;
  }

  const beds = parseBedConfiguration(formData.get("bed_configuration"));
  if (!beds.valid) {
    return beds;
  }

  return {
    valid: true,
    value: {
      capacity_adults: capacity.value,
      size_sqm: size.value,
      bed_configuration: beds.value,
    },
  };
}

// What the agent needs before it can honestly offer a hotel to a customer.
// Not a DB constraint: the columns are nullable so the migration could run
// against rows that predate them (see 0023's comment). This is what the
// dashboard flags instead, so an incomplete hotel is visible rather than
// silently unsellable.
export function missingHotelProfileFields(hotel: {
  distance_to_haram_meters: number | null;
  star_rating: number | null;
  address_text: string | null;
}): string[] {
  const missing: string[] = [];
  if (hotel.distance_to_haram_meters === null) {
    missing.push("المسافة عن الحرم");
  }
  if (hotel.star_rating === null) {
    missing.push("التصنيف");
  }
  if (hotel.address_text === null) {
    missing.push("العنوان");
  }
  return missing;
}
