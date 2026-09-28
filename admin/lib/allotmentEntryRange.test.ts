import { describe, expect, it } from "vitest";
import {
  MAX_ALLOTMENT_ENTRY_RANGE_NIGHTS,
  nightCount,
  validateAllotmentEntryRange,
} from "./allotmentEntryRange";

describe("nightCount", () => {
  it("counts a single-night range as 1", () => {
    expect(nightCount("2027-01-01", "2027-01-01")).toBe(1);
  });

  it("counts an inclusive multi-night range correctly", () => {
    expect(nightCount("2027-01-01", "2027-01-03")).toBe(3);
  });

  it("is not thrown off by a DST transition in the local timezone", () => {
    expect(nightCount("2027-03-01", "2027-03-31")).toBe(31);
  });
});

describe("validateAllotmentEntryRange", () => {
  it("accepts a valid range and values", () => {
    const result = validateAllotmentEntryRange("2027-01-01", "2027-01-10", 20, 250);
    expect(result).toEqual({ valid: true, message: "" });
  });

  it("rejects a missing date", () => {
    expect(validateAllotmentEntryRange("", "2027-01-10", 20, 250).valid).toBe(false);
  });

  it("rejects a last night before the first night", () => {
    const result = validateAllotmentEntryRange("2027-01-10", "2027-01-01", 20, 250);
    expect(result.valid).toBe(false);
  });

  it("accepts a range exactly at the night cap", () => {
    // 2027-01-01 + 365 days = 2028-01-01, a 366-night inclusive range
    // (2028 is a leap year, but the count is by elapsed days, not calendar
    // years, so that doesn't change the boundary math here).
    const lastNight = "2028-01-01";
    expect(nightCount("2027-01-01", lastNight)).toBe(MAX_ALLOTMENT_ENTRY_RANGE_NIGHTS);
    const result = validateAllotmentEntryRange("2027-01-01", lastNight, 20, 250);
    expect(result.valid).toBe(true);
  });

  it("rejects a range one night past the cap", () => {
    const lastNight = "2028-01-02";
    expect(nightCount("2027-01-01", lastNight)).toBe(MAX_ALLOTMENT_ENTRY_RANGE_NIGHTS + 1);
    const result = validateAllotmentEntryRange("2027-01-01", lastNight, 20, 250);
    expect(result.valid).toBe(false);
    expect(result.message).toContain("367");
  });

  it("rejects a negative room count", () => {
    const result = validateAllotmentEntryRange("2027-01-01", "2027-01-01", -1, 250);
    expect(result.valid).toBe(false);
  });

  it("rejects a non-integer room count", () => {
    const result = validateAllotmentEntryRange("2027-01-01", "2027-01-01", 20.5, 250);
    expect(result.valid).toBe(false);
  });

  it("accepts zero rooms (closing a night)", () => {
    const result = validateAllotmentEntryRange("2027-01-01", "2027-01-01", 0, 250);
    expect(result.valid).toBe(true);
  });

  it("rejects a negative cost", () => {
    const result = validateAllotmentEntryRange("2027-01-01", "2027-01-01", 20, -1);
    expect(result.valid).toBe(false);
  });

  it("rejects a non-integer cost", () => {
    const result = validateAllotmentEntryRange("2027-01-01", "2027-01-01", 20, 12.5);
    expect(result.valid).toBe(false);
  });
});
