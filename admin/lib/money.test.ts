import { describe, expect, it } from "vitest";
import { formatHalalasAsRiyal, halalasToRiyals, riyalsToHalalas } from "./money";

describe("riyalsToHalalas", () => {
  it("converts a whole riyal amount to halalas", () => {
    expect(riyalsToHalalas(250)).toBe(25_000);
  });

  it("converts zero", () => {
    expect(riyalsToHalalas(0)).toBe(0);
  });

  it("rejects a negative amount", () => {
    expect(() => riyalsToHalalas(-1)).toThrow(RangeError);
  });

  it("rejects a fractional amount", () => {
    expect(() => riyalsToHalalas(12.5)).toThrow(RangeError);
  });
});

describe("halalasToRiyals", () => {
  it("converts an exact multiple of 100 back to whole riyals", () => {
    expect(halalasToRiyals(25_000)).toBe(250);
  });

  it("converts zero", () => {
    expect(halalasToRiyals(0)).toBe(0);
  });

  it("round-trips with riyalsToHalalas", () => {
    expect(halalasToRiyals(riyalsToHalalas(777))).toBe(777);
  });

  it("rejects a negative amount", () => {
    expect(() => halalasToRiyals(-100)).toThrow(RangeError);
  });

  it("rejects an amount that is not a whole number of riyals", () => {
    expect(() => halalasToRiyals(1_250)).toThrow(RangeError);
  });
});

describe("formatHalalasAsRiyal", () => {
  it("renders riyals and halalas with a thousands separator", () => {
    expect(formatHalalasAsRiyal(125_000)).toBe("1,250.00 ريال");
    expect(formatHalalasAsRiyal(68_770)).toBe("687.70 ريال");
    expect(formatHalalasAsRiyal(5)).toBe("0.05 ريال");
    expect(formatHalalasAsRiyal(0)).toBe("0.00 ريال");
  });

  it("rejects a negative or fractional amount", () => {
    expect(() => formatHalalasAsRiyal(-1)).toThrow(RangeError);
    expect(() => formatHalalasAsRiyal(1.5)).toThrow(RangeError);
  });
});
