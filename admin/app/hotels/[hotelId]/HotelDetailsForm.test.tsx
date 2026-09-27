import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { HotelDetailsForm } from "./HotelDetailsForm";
import { MAX_DISTRICT_NAME_LENGTH } from "@/lib/hotelDetails";
import type { Hotel } from "@/lib/types";

const RECORDED_HOTEL: Hotel = {
  id: 1,
  hotel_name: "فندق الاختبار",
  created_at: "2026-08-01T00:00:00Z",
  distance_to_haram_meters: 450,
  star_rating: 4,
  address_text: "شارع إبراهيم الخليل",
  check_in_time: "15:00",
  check_out_time: "12:00",
  city: "makkah",
  zone: "makkah_central",
  district_name: "العزيزية",
  weekend_days: [5, 6],
  is_active: true,
};

const UNRECORDED_HOTEL: Hotel = {
  ...RECORDED_HOTEL,
  city: null,
  zone: null,
  district_name: null,
};

function formHtml(hotel: Hotel): string {
  return renderToStaticMarkup(
    <HotelDetailsForm hotel={hotel} selectedAmenities={[]} canEdit={true} />,
  );
}

function summaryHtml(hotel: Hotel): string {
  return renderToStaticMarkup(
    <HotelDetailsForm hotel={hotel} selectedAmenities={[]} canEdit={false} />,
  );
}

function tickedWeekendDays(html: string): string[] {
  return [...html.matchAll(/name="weekend_days" checked="" value="(\d)"/g)].map(
    (match) => match[1],
  );
}

describe("HotelDetailsForm location fields", () => {
  it("renders the city select with the recorded city selected", () => {
    const html = formHtml(RECORDED_HOTEL);
    expect(html).toContain('name="city"');
    expect(html).toMatch(/<option value="makkah" selected="">/);
    expect(html).toContain("مكة المكرمة");
    expect(html).toContain("المدينة المنورة");
  });

  it("groups all seven zones under their two cities in the zone select", () => {
    const html = formHtml(RECORDED_HOTEL);
    const zoneSelectStart = html.indexOf('name="zone"');
    const zoneSelectEnd = html.indexOf("</select>", zoneSelectStart);
    const zoneSelect = html.slice(zoneSelectStart, zoneSelectEnd);

    expect(zoneSelect).toContain('<optgroup label="مكة المكرمة">');
    expect(zoneSelect).toContain('<optgroup label="المدينة المنورة">');
    expect(zoneSelect.match(/<option value="(makkah|madinah)_/g)).toHaveLength(7);
    expect(zoneSelect).toMatch(/<option value="makkah_central" selected="">/);
  });

  it("selects nothing when no city or zone is recorded", () => {
    const html = formHtml(UNRECORDED_HOTEL);
    expect(html).toMatch(/<option value="" selected="">غير محدد<\/option>/);
  });

  it("limits the district name to the database's maximum length", () => {
    const html = formHtml(RECORDED_HOTEL);
    expect(html).toContain(`maxLength="${MAX_DISTRICT_NAME_LENGTH}"`);
    expect(html).toContain('value="العزيزية"');
  });

  it("offers all seven weekdays and ticks the hotel's own weekend", () => {
    const html = formHtml(RECORDED_HOTEL);
    expect(html.match(/name="weekend_days"/g)).toHaveLength(7);
    expect(tickedWeekendDays(html).sort()).toEqual(["5", "6"]);
  });

  it("ticks whatever weekend is recorded, not always Friday and Saturday", () => {
    const html = formHtml({ ...RECORDED_HOTEL, weekend_days: [4, 5] });
    expect(tickedWeekendDays(html).sort()).toEqual(["4", "5"]);
  });
});

describe("HotelDetailsForm read-only summary", () => {
  it("shows the recorded location, district and weekend as labels", () => {
    const html = summaryHtml(RECORDED_HOTEL);
    expect(html).toContain("مكة المكرمة");
    expect(html).toContain("المنطقة المركزية (حول الحرم)");
    expect(html).toContain("العزيزية");
    // Listed in the order the week is read in Arabic, Saturday first.
    expect(html).toContain("السبت، الجمعة");
  });

  it("shows a dash for a location that was never recorded", () => {
    const html = summaryHtml(UNRECORDED_HOTEL);
    const cityRow = html.slice(html.indexOf("المدينة</dt>"));
    expect(cityRow.slice(0, cityRow.indexOf("</div>"))).toContain("—");
  });
});
