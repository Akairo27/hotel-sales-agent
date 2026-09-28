import { describe, expect, it } from "vitest";
import {
  HOTEL_ZONES,
  MAX_CAPACITY_ADULTS,
  MAX_DISTRICT_NAME_LENGTH,
  MAX_STAR_RATING,
  missingHotelProfileFields,
  parseAmenities,
  parseBedConfiguration,
  parseCity,
  parseDistrictName,
  parseHotelDetails,
  parseOptionalInteger,
  parseOptionalTime,
  parseRoomTypeDetails,
  parseWeekendDays,
  parseZone,
  ZONE_LABELS,
  zoneBelongsToCity,
} from "@/lib/hotelDetails";

function formDataFrom(entries: [string, string][]): FormData {
  const formData = new FormData();
  for (const [key, value] of entries) {
    formData.append(key, value);
  }
  return formData;
}

const COMPLETE_HOTEL_FORM: [string, string][] = [
  ["distance_to_haram_meters", "450"],
  ["star_rating", "4"],
  ["address_text", "  شارع إبراهيم الخليل  "],
  ["check_in_time", "15:00"],
  ["check_out_time", "12:00"],
  ["city", "makkah"],
  ["zone", "makkah_central"],
  ["district_name", "  العزيزية  "],
  // Submitted out of order on purpose: the parser sorts them.
  ["weekend_days", "6"],
  ["weekend_days", "5"],
  ["is_active", "on"],
];

describe("parseOptionalInteger", () => {
  const bounds = { label: "التصنيف", min: 1, max: 5 };

  it("treats an empty or whitespace-only field as not-recorded, not as zero", () => {
    // The distinction that matters: Number("") is 0, so a blank field that
    // fell through to Number() would be stored as a 0 the DB then rejects.
    expect(parseOptionalInteger("", bounds)).toEqual({ valid: true, value: null });
    expect(parseOptionalInteger("   ", bounds)).toEqual({ valid: true, value: null });
    expect(parseOptionalInteger(null, bounds)).toEqual({ valid: true, value: null });
  });

  it("accepts an in-range integer, trimmed", () => {
    expect(parseOptionalInteger(" 4 ", bounds)).toEqual({ valid: true, value: 4 });
  });

  it("rejects a non-integer", () => {
    expect(parseOptionalInteger("4.5", bounds).valid).toBe(false);
    expect(parseOptionalInteger("abc", bounds).valid).toBe(false);
  });

  it("rejects a value outside the bounds and names the range", () => {
    const tooHigh = parseOptionalInteger("6", bounds);
    expect(tooHigh.valid).toBe(false);
    if (!tooHigh.valid) {
      expect(tooHigh.message).toContain("بين 1 و5");
    }
    expect(parseOptionalInteger("0", bounds).valid).toBe(false);
  });

  it("names an open-ended range when no maximum is set", () => {
    const result = parseOptionalInteger("0", { label: "المسافة عن الحرم", min: 1 });
    expect(result.valid).toBe(false);
    if (!result.valid) {
      expect(result.message).toContain("1 أو أكثر");
    }
  });
});

describe("parseOptionalTime", () => {
  it("accepts HH:MM and HH:MM:SS", () => {
    expect(parseOptionalTime("15:00", "وقت الدخول")).toEqual({
      valid: true,
      value: "15:00",
    });
    expect(parseOptionalTime("15:00:30", "وقت الدخول")).toEqual({
      valid: true,
      value: "15:00:30",
    });
  });

  it("treats an empty field as not-recorded", () => {
    expect(parseOptionalTime("", "وقت الدخول")).toEqual({ valid: true, value: null });
  });

  it("rejects an out-of-range or malformed time", () => {
    expect(parseOptionalTime("24:00", "وقت الدخول").valid).toBe(false);
    expect(parseOptionalTime("15:60", "وقت الدخول").valid).toBe(false);
    expect(parseOptionalTime("3pm", "وقت الدخول").valid).toBe(false);
  });
});

describe("parseAmenities", () => {
  it("accepts a subset of the known list", () => {
    expect(parseAmenities(["wifi", "haram_view"])).toEqual({
      valid: true,
      value: ["wifi", "haram_view"],
    });
  });

  it("accepts an empty selection", () => {
    expect(parseAmenities([])).toEqual({ valid: true, value: [] });
  });

  it("rejects a value outside the closed list", () => {
    expect(parseAmenities(["wifi", "helipad"]).valid).toBe(false);
  });

  it("rejects a duplicate, which this form cannot legitimately produce", () => {
    expect(parseAmenities(["wifi", "wifi"]).valid).toBe(false);
  });
});

describe("parseBedConfiguration", () => {
  it("accepts a known configuration and an empty one", () => {
    expect(parseBedConfiguration("twin")).toEqual({ valid: true, value: "twin" });
    expect(parseBedConfiguration("")).toEqual({ valid: true, value: null });
  });

  it("rejects an unknown configuration", () => {
    expect(parseBedConfiguration("king").valid).toBe(false);
  });
});

describe("parseHotelDetails", () => {
  it("parses a complete form, trimming the address", () => {
    const result = parseHotelDetails(
      formDataFrom([...COMPLETE_HOTEL_FORM, ["amenities", "wifi"]]),
    );
    expect(result.valid).toBe(true);
    if (!result.valid) {
      return;
    }
    expect(result.value.patch).toEqual({
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
    });
    expect(result.value.amenities).toEqual(["wifi"]);
  });

  it("reads an absent is_active checkbox as false, not as unchanged", () => {
    const withoutFlag = COMPLETE_HOTEL_FORM.filter(([key]) => key !== "is_active");
    const result = parseHotelDetails(formDataFrom(withoutFlag));
    expect(result.valid).toBe(true);
    if (result.valid) {
      expect(result.value.patch.is_active).toBe(false);
    }
  });

  it("stores an all-blank form as nulls rather than refusing it", () => {
    const result = parseHotelDetails(
      formDataFrom([
        ["distance_to_haram_meters", ""],
        ["star_rating", ""],
        ["address_text", "   "],
        ["check_in_time", ""],
        ["check_out_time", ""],
        ["city", ""],
        ["zone", ""],
        ["district_name", "   "],
        ["weekend_days", "5"],
        ["weekend_days", "6"],
      ]),
    );
    expect(result.valid).toBe(true);
    if (result.valid) {
      expect(result.value.patch).toEqual({
        distance_to_haram_meters: null,
        star_rating: null,
        address_text: null,
        check_in_time: null,
        check_out_time: null,
        city: null,
        zone: null,
        district_name: null,
        weekend_days: [5, 6],
        is_active: false,
      });
    }
  });

  it("refuses a form with a zone that does not belong to the chosen city", () => {
    const wrongZone = COMPLETE_HOTEL_FORM.map(([key, value]): [string, string] =>
      key === "zone" ? [key, "madinah_north"] : [key, value],
    );
    const result = parseHotelDetails(formDataFrom(wrongZone));
    expect(result.valid).toBe(false);
    if (!result.valid) {
      expect(result.message).toContain("المنطقة");
    }
  });

  it("refuses a form with no weekend day ticked", () => {
    const withoutWeekend = COMPLETE_HOTEL_FORM.filter(([key]) => key !== "weekend_days");
    const result = parseHotelDetails(formDataFrom(withoutWeekend));
    expect(result.valid).toBe(false);
    if (!result.valid) {
      expect(result.message).toContain("أيام العطلة");
    }
  });

  it("reports the first invalid field instead of saving a partial row", () => {
    const result = parseHotelDetails(
      formDataFrom([
        ["distance_to_haram_meters", "450"],
        ["star_rating", String(MAX_STAR_RATING + 1)],
      ]),
    );
    expect(result.valid).toBe(false);
    if (!result.valid) {
      expect(result.message).toContain("التصنيف");
    }
  });
});

describe("parseRoomTypeDetails", () => {
  it("parses a complete form", () => {
    const result = parseRoomTypeDetails(
      formDataFrom([
        ["capacity_adults", "3"],
        ["size_sqm", "28"],
        ["bed_configuration", "triple"],
      ]),
    );
    expect(result).toEqual({
      valid: true,
      value: { capacity_adults: 3, size_sqm: 28, bed_configuration: "triple" },
    });
  });

  it("rejects a capacity above the constraint's ceiling", () => {
    const result = parseRoomTypeDetails(
      formDataFrom([["capacity_adults", String(MAX_CAPACITY_ADULTS + 1)]]),
    );
    expect(result.valid).toBe(false);
  });
});

describe("missingHotelProfileFields", () => {
  it("returns nothing for a complete profile", () => {
    expect(
      missingHotelProfileFields({
        distance_to_haram_meters: 450,
        star_rating: 4,
        address_text: "شارع إبراهيم الخليل",
        city: "makkah",
        zone: "makkah_central",
      }),
    ).toEqual([]);
  });

  it("names every field the agent would need and does not have", () => {
    expect(
      missingHotelProfileFields({
        distance_to_haram_meters: null,
        star_rating: null,
        address_text: null,
        city: null,
        zone: null,
      }),
    ).toEqual(["المسافة عن الحرم", "التصنيف", "العنوان", "المدينة", "المنطقة"]);
  });

  it("does not treat a zero-distance as missing", () => {
    // 0 is falsy; only an explicit null means "not recorded". The DB
    // rejects 0 outright, so this guards the check itself, not the value.
    expect(
      missingHotelProfileFields({
        distance_to_haram_meters: 0,
        star_rating: 4,
        address_text: "x",
        city: "makkah",
        zone: "makkah_central",
      }),
    ).toEqual([]);
  });

  it("names a missing city on its own", () => {
    expect(
      missingHotelProfileFields({
        distance_to_haram_meters: 450,
        star_rating: 4,
        address_text: "شارع إبراهيم الخليل",
        city: null,
        zone: "makkah_central",
      }),
    ).toEqual(["المدينة"]);
  });

  it("names a missing zone on its own", () => {
    expect(
      missingHotelProfileFields({
        distance_to_haram_meters: 450,
        star_rating: 4,
        address_text: "شارع إبراهيم الخليل",
        city: "makkah",
        zone: null,
      }),
    ).toEqual(["المنطقة"]);
  });
});

describe("parseCity", () => {
  it("accepts each city and an empty field", () => {
    expect(parseCity("makkah")).toEqual({ valid: true, value: "makkah" });
    expect(parseCity(" madinah ")).toEqual({ valid: true, value: "madinah" });
    expect(parseCity("")).toEqual({ valid: true, value: null });
    expect(parseCity(null)).toEqual({ valid: true, value: null });
  });

  it("rejects a city outside the closed list, including a differently cased one", () => {
    expect(parseCity("jeddah").valid).toBe(false);
    expect(parseCity("Makkah").valid).toBe(false);
  });
});

describe("parseZone", () => {
  it("accepts a zone that belongs to the chosen city", () => {
    expect(parseZone("makkah_outside", "makkah")).toEqual({
      valid: true,
      value: "makkah_outside",
    });
    expect(parseZone("madinah_south", "madinah")).toEqual({
      valid: true,
      value: "madinah_south",
    });
  });

  it("treats an empty zone as not recorded, with or without a city", () => {
    expect(parseZone("", "makkah")).toEqual({ valid: true, value: null });
    expect(parseZone("", null)).toEqual({ valid: true, value: null });
  });

  it("rejects a zone of the other city and names the field", () => {
    const result = parseZone("madinah_central", "makkah");
    expect(result.valid).toBe(false);
    if (!result.valid) {
      expect(result.message).toContain("المنطقة");
    }
  });

  it("rejects a zone when no city is chosen", () => {
    const result = parseZone("makkah_central", null);
    expect(result.valid).toBe(false);
    if (!result.valid) {
      expect(result.message).toContain("المدينة");
    }
  });

  it("rejects a zone outside the closed list", () => {
    expect(parseZone("makkah_north", "makkah").valid).toBe(false);
  });

  it("agrees with zoneBelongsToCity for every zone and city", () => {
    for (const zone of HOTEL_ZONES) {
      expect(zoneBelongsToCity(zone, zone.startsWith("makkah") ? "makkah" : "madinah")).toBe(
        true,
      );
      expect(zoneBelongsToCity(zone, zone.startsWith("makkah") ? "madinah" : "makkah")).toBe(
        false,
      );
    }
  });
});

describe("parseWeekendDays", () => {
  it("returns the ticked days sorted", () => {
    expect(parseWeekendDays(["6", "5"])).toEqual({ valid: true, value: [5, 6] });
    expect(parseWeekendDays(["7"])).toEqual({ valid: true, value: [7] });
  });

  it("accepts all seven days", () => {
    expect(parseWeekendDays(["1", "2", "3", "4", "5", "6", "7"])).toEqual({
      valid: true,
      value: [1, 2, 3, 4, 5, 6, 7],
    });
  });

  it("refuses an empty selection, which this form never means", () => {
    const result = parseWeekendDays([]);
    expect(result.valid).toBe(false);
    if (!result.valid) {
      expect(result.message).toContain("يوماً واحداً");
    }
  });

  it("rejects a value that is not an ISO weekday", () => {
    for (const bad of ["0", "8", "-1", "5.5", "Friday", " 5", ""]) {
      expect(parseWeekendDays([bad]).valid, `value ${JSON.stringify(bad)}`).toBe(false);
    }
  });

  it("rejects a duplicate, which a checkbox group cannot legitimately produce", () => {
    expect(parseWeekendDays(["5", "5"]).valid).toBe(false);
  });
});

describe("parseDistrictName", () => {
  it("trims a name and treats a blank field as not recorded", () => {
    expect(parseDistrictName("  العزيزية ")).toEqual({ valid: true, value: "العزيزية" });
    expect(parseDistrictName("   ")).toEqual({ valid: true, value: null });
    expect(parseDistrictName(null)).toEqual({ valid: true, value: null });
  });

  it("accepts exactly the maximum length and rejects one more", () => {
    const longest = "م".repeat(MAX_DISTRICT_NAME_LENGTH);
    expect(parseDistrictName(longest)).toEqual({ valid: true, value: longest });
    expect(parseDistrictName(longest + "م").valid).toBe(false);
  });

  it("counts characters the way Postgres does, not UTF-16 units", () => {
    // Each of these is one character to Postgres but two UTF-16 units.
    const astralAtTheLimit = "\u{1F3E8}".repeat(MAX_DISTRICT_NAME_LENGTH);
    expect(parseDistrictName(astralAtTheLimit).valid).toBe(true);
    expect(parseDistrictName(astralAtTheLimit + "\u{1F3E8}").valid).toBe(false);
  });
});

describe("ZONE_LABELS", () => {
  it("words the three Madinah directional zones as full zone names, as the owner asked", () => {
    // A bare "الشمال" does not say north of what, so each direction reads as
    // a zone: the owner's wording of 2026-09-27.
    expect(ZONE_LABELS.madinah_north).toBe("المنطقة الشمالية");
    expect(ZONE_LABELS.madinah_west).toBe("المنطقة الغربية");
    expect(ZONE_LABELS.madinah_south).toBe("المنطقة الجنوبية");
  });

  it("leaves the other four zone labels as they were", () => {
    expect(ZONE_LABELS.makkah_central).toBe("المنطقة المركزية (حول الحرم)");
    expect(ZONE_LABELS.makkah_outside).toBe("خارج المنطقة المركزية");
    expect(ZONE_LABELS.madinah_central).toBe("المنطقة المركزية (حول المسجد النبوي)");
    expect(ZONE_LABELS.madinah_outside).toBe("خارج المنطقة المركزية");
  });
});
