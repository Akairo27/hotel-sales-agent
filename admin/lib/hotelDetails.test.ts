import { describe, expect, it } from "vitest";
import {
  MAX_CAPACITY_ADULTS,
  MAX_STAR_RATING,
  missingHotelProfileFields,
  parseAmenities,
  parseBedConfiguration,
  parseHotelDetails,
  parseOptionalInteger,
  parseOptionalTime,
  parseRoomTypeDetails,
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
        is_active: false,
      });
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
      }),
    ).toEqual([]);
  });

  it("names every field the agent would need and does not have", () => {
    expect(
      missingHotelProfileFields({
        distance_to_haram_meters: null,
        star_rating: null,
        address_text: null,
      }),
    ).toEqual(["المسافة عن الحرم", "التصنيف", "العنوان"]);
  });

  it("does not treat a zero-distance as missing", () => {
    // 0 is falsy; only an explicit null means "not recorded". The DB
    // rejects 0 outright, so this guards the check itself, not the value.
    expect(
      missingHotelProfileFields({
        distance_to_haram_meters: 0,
        star_rating: 4,
        address_text: "x",
      }),
    ).toEqual([]);
  });
});
