import type { Hotel } from "@/lib/types";
import {
  AMENITY_LABELS,
  HOTEL_AMENITIES,
  MAX_STAR_RATING,
  MIN_STAR_RATING,
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
