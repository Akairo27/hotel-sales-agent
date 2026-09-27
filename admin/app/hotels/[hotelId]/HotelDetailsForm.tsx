import type { Hotel } from "@/lib/types";
import {
  AMENITY_LABELS,
  CITY_LABELS,
  HOTEL_AMENITIES,
  HOTEL_CITIES,
  HOTEL_ZONES,
  MAX_DISTRICT_NAME_LENGTH,
  MAX_STAR_RATING,
  MIN_STAR_RATING,
  WEEKDAY_DISPLAY_ORDER,
  WEEKDAY_LABELS,
  ZONE_LABELS,
  zoneBelongsToCity,
  type HotelAmenity,
} from "@/lib/hotelDetails";
import {
  ACTION_BAR,
  BUTTON_PRIMARY,
  CARD,
  CHECKBOX,
  CHECKBOX_LABEL,
  FIELDSET,
  HINT,
  INPUT,
  LABEL,
  LEGEND,
  SECTION_TITLE,
  SELECT,
} from "@/lib/ui";
import { updateHotelDetails } from "./actions";

// Every field is optional in the database (migration 0023) but marked
// required in this form: the columns are nullable so the migration could
// run against pre-existing rows, not because a hotel the agent will sell
// is allowed to have no distance from the Haram. The page's own
// missingHotelProfileFields banner is what surfaces the rows that predate
// this screen; `required` is what stops new blanks being introduced.
export function HotelDetailsForm({
  hotel,
  selectedAmenities,
  canEdit,
}: {
  hotel: Hotel;
  selectedAmenities: HotelAmenity[];
  canEdit: boolean;
}) {
  if (!canEdit) {
    return <HotelDetailsSummary hotel={hotel} selectedAmenities={selectedAmenities} />;
  }

  const saveThisHotelsDetails = updateHotelDetails.bind(null, hotel.id);
  const selected = new Set(selectedAmenities);
  const weekendDays = new Set<number>(hotel.weekend_days);

  return (
    <section className={CARD}>
      <h2 className={SECTION_TITLE}>تفاصيل الفندق</h2>
      <form action={saveThisHotelsDetails} className="mt-4">
        <div className="flex flex-wrap gap-4">
          <div className="min-w-40 flex-1">
            <label htmlFor="distance_to_haram_meters" className={LABEL}>
              المسافة عن الحرم (متر)
            </label>
            <input
              id="distance_to_haram_meters"
              name="distance_to_haram_meters"
              type="number"
              min={1}
              step={1}
              required
              defaultValue={hotel.distance_to_haram_meters ?? ""}
              className={`${INPUT} mt-1 w-full`}
            />
          </div>

          <div className="min-w-40 flex-1">
            <label htmlFor="star_rating" className={LABEL}>
              التصنيف (نجوم)
            </label>
            <input
              id="star_rating"
              name="star_rating"
              type="number"
              min={MIN_STAR_RATING}
              max={MAX_STAR_RATING}
              step={1}
              required
              defaultValue={hotel.star_rating ?? ""}
              className={`${INPUT} mt-1 w-full`}
            />
          </div>
        </div>

        <div className="mt-4">
          <label htmlFor="address_text" className={LABEL}>
            العنوان
          </label>
          <input
            id="address_text"
            name="address_text"
            required
            defaultValue={hotel.address_text ?? ""}
            className={`${INPUT} mt-1 w-full`}
          />
        </div>

        <fieldset className={`${FIELDSET} mt-6`}>
          <legend className={LEGEND}>الموقع</legend>
          <div className="flex flex-wrap gap-4">
            <div className="min-w-40 flex-1">
              <label htmlFor="city" className={LABEL}>
                المدينة
              </label>
              <select
                id="city"
                name="city"
                defaultValue={hotel.city ?? ""}
                className={`${SELECT} mt-1 w-full`}
              >
                <option value="">غير محدد</option>
                {HOTEL_CITIES.map((city) => (
                  <option key={city} value={city}>
                    {CITY_LABELS[city]}
                  </option>
                ))}
              </select>
            </div>
            <div className="min-w-40 flex-1">
              <label htmlFor="zone" className={LABEL}>
                المنطقة
              </label>
              <select
                id="zone"
                name="zone"
                defaultValue={hotel.zone ?? ""}
                className={`${SELECT} mt-1 w-full`}
              >
                <option value="">غير محدد</option>
                {HOTEL_CITIES.map((city) => (
                  <optgroup key={city} label={CITY_LABELS[city]}>
                    {HOTEL_ZONES.filter((zone) => zoneBelongsToCity(zone, city)).map(
                      (zone) => (
                        <option key={zone} value={zone}>
                          {ZONE_LABELS[zone]}
                        </option>
                      ),
                    )}
                  </optgroup>
                ))}
              </select>
            </div>
          </div>
          <div className="mt-4">
            <label htmlFor="district_name" className={LABEL}>
              الحي أو الشارع
            </label>
            <input
              id="district_name"
              name="district_name"
              maxLength={MAX_DISTRICT_NAME_LENGTH}
              defaultValue={hotel.district_name ?? ""}
              className={`${INPUT} mt-1 w-full`}
            />
          </div>
          <p className={`${HINT} mt-2`}>
            اختياري. المنطقة يجب أن تتبع المدينة المختارة. اسم الحي اسم علم يُعرض كما
            هو ولا يُستعمل تعليمةً، وطوله حتى {MAX_DISTRICT_NAME_LENGTH} حرفاً.
          </p>
        </fieldset>

        <fieldset className={`${FIELDSET} mt-6`}>
          <legend className={LEGEND}>العطلة الأسبوعية</legend>
          <p className={HINT}>
            الأيام التي تُعدّ عطلة أسبوعية لهذا الفندق. الافتراضي الجمعة والسبت، ويلزم
            اختيار يوم واحد على الأقل.
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            {WEEKDAY_DISPLAY_ORDER.map((day) => (
              <label key={day} className={CHECKBOX_LABEL}>
                <input
                  type="checkbox"
                  name="weekend_days"
                  value={day}
                  defaultChecked={weekendDays.has(day)}
                  className={CHECKBOX}
                />
                {WEEKDAY_LABELS[day]}
              </label>
            ))}
          </div>
        </fieldset>

        <fieldset className={`${FIELDSET} mt-6`}>
          <legend className={LEGEND}>أوقات الدخول والخروج</legend>
          <div className="flex flex-wrap gap-4">
            <div className="min-w-40 flex-1">
              <label htmlFor="check_in_time" className={LABEL}>
                وقت الدخول
              </label>
              <input
                id="check_in_time"
                name="check_in_time"
                type="time"
                defaultValue={hotel.check_in_time ?? ""}
                className={`${INPUT} mt-1 w-full`}
              />
            </div>
            <div className="min-w-40 flex-1">
              <label htmlFor="check_out_time" className={LABEL}>
                وقت الخروج
              </label>
              <input
                id="check_out_time"
                name="check_out_time"
                type="time"
                defaultValue={hotel.check_out_time ?? ""}
                className={`${INPUT} mt-1 w-full`}
              />
            </div>
          </div>
        </fieldset>

        <fieldset className={`${FIELDSET} mt-6`}>
          <legend className={LEGEND}>المرافق</legend>
          <p className={HINT}>
            قائمة مغلقة — الوكيل لا يذكر إلا ما هو مُحدَّد هنا. إضافة مرفق جديد تتم
            بميقريشن.
          </p>
          <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {HOTEL_AMENITIES.map((amenity) => (
              <label key={amenity} className={CHECKBOX_LABEL}>
                <input
                  type="checkbox"
                  name="amenities"
                  value={amenity}
                  defaultChecked={selected.has(amenity)}
                  className={CHECKBOX}
                />
                {AMENITY_LABELS[amenity]}
              </label>
            ))}
          </div>
        </fieldset>

        <fieldset className={`${FIELDSET} mt-6`}>
          <legend className={LEGEND}>الحالة</legend>
          <label className={CHECKBOX_LABEL}>
            <input
              type="checkbox"
              name="is_active"
              defaultChecked={hotel.is_active}
              className={CHECKBOX}
            />
            الفندق مفعّل ويُعرض على العملاء
          </label>
          <p className={`${HINT} mt-2`}>
            إيقاف الفندق هو البديل عن حذفه — الحذف غير مسموح لأي دور، فالحصص والحجوزات
            تشير إليه.
          </p>
        </fieldset>

        <div className={ACTION_BAR}>
          <button type="submit" className={BUTTON_PRIMARY}>
            حفظ التفاصيل
          </button>
        </div>
      </form>
    </section>
  );
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd className="mt-1 text-sm text-foreground">{value}</dd>
    </div>
  );
}

const NOT_RECORDED = "—";

function weekendDaysLabel(weekendDays: readonly number[]): string {
  const names = WEEKDAY_DISPLAY_ORDER.filter((day) => weekendDays.includes(day)).map(
    (day) => WEEKDAY_LABELS[day],
  );
  return names.length === 0 ? NOT_RECORDED : names.join("، ");
}

// Sales can read a hotel's profile but not change it (migration 0014's
// split), so the read-only view is a description list, not a disabled
// form — a form nobody can submit reads as broken rather than as
// intentionally not theirs to edit.
function HotelDetailsSummary({
  hotel,
  selectedAmenities,
}: {
  hotel: Hotel;
  selectedAmenities: HotelAmenity[];
}) {
  return (
    <section className={CARD}>
      <h2 className={SECTION_TITLE}>تفاصيل الفندق</h2>
      <dl className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <DetailRow
          label="المسافة عن الحرم"
          value={
            hotel.distance_to_haram_meters === null
              ? NOT_RECORDED
              : `${hotel.distance_to_haram_meters} متر`
          }
        />
        <DetailRow
          label="التصنيف"
          value={hotel.star_rating === null ? NOT_RECORDED : `${hotel.star_rating} نجوم`}
        />
        <DetailRow label="العنوان" value={hotel.address_text ?? NOT_RECORDED} />
        <DetailRow
          label="المدينة"
          value={hotel.city === null ? NOT_RECORDED : CITY_LABELS[hotel.city]}
        />
        <DetailRow
          label="المنطقة"
          value={hotel.zone === null ? NOT_RECORDED : ZONE_LABELS[hotel.zone]}
        />
        <DetailRow label="الحي أو الشارع" value={hotel.district_name ?? NOT_RECORDED} />
        <DetailRow label="العطلة الأسبوعية" value={weekendDaysLabel(hotel.weekend_days)} />
        <DetailRow label="وقت الدخول" value={hotel.check_in_time ?? NOT_RECORDED} />
        <DetailRow label="وقت الخروج" value={hotel.check_out_time ?? NOT_RECORDED} />
        <DetailRow
          label="المرافق"
          value={
            selectedAmenities.length === 0
              ? NOT_RECORDED
              : selectedAmenities.map((amenity) => AMENITY_LABELS[amenity]).join("، ")
          }
        />
      </dl>
    </section>
  );
}
