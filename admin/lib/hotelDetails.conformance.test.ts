// Pins admin/lib/hotelDetails.ts's closed lists to the CHECK constraints in
// db/migrations/0023_hotel_details.sql and 0028_hotel_location.sql that
// actually enforce them.
//
// Same purpose as seasonCalendar.conformance.test.ts, by a different route:
// there is no generated fixture here because the ground truth is plain SQL,
// so the migration is read and its IN (...) lists parsed directly. Adding an
// amenity to one side and not the other fails this test rather than showing
// up as a save that is rejected by Postgres with an English error.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  BED_CONFIGURATIONS,
  CITY_LABELS,
  DEFAULT_WEEKEND_DAYS,
  HOTEL_AMENITIES,
  HOTEL_CITIES,
  HOTEL_ZONES,
  ISO_WEEKDAYS,
  MAX_DISTRICT_NAME_LENGTH,
  WEEKDAY_LABELS,
  ZONE_LABELS,
} from "@/lib/hotelDetails";

function readMigration(fileName: string): string {
  return readFileSync(
    fileURLToPath(new URL(`../../db/migrations/${fileName}`, import.meta.url)),
    "utf8",
  );
}

const migrationSql = readMigration("0023_hotel_details.sql");
const locationMigrationSql = readMigration("0028_hotel_location.sql");

/** Returns the quoted values of the `IN (...)` list attached to a named
 * CHECK constraint, in the order the migration writes them. */
function checkConstraintValues(
  constraintName: string,
  sqlText: string = migrationSql,
): string[] {
  const constraintIndex = sqlText.indexOf(constraintName);
  expect(
    constraintIndex,
    `constraint ${constraintName} not found in the migration`,
  ).toBeGreaterThan(-1);

  const inIndex = sqlText.indexOf("IN (", constraintIndex);
  expect(inIndex, `no IN (...) list after ${constraintName}`).toBeGreaterThan(-1);

  const closingIndex = sqlText.indexOf(")", inIndex + "IN (".length);
  const list = sqlText.slice(inIndex + "IN (".length, closingIndex);
  return [...list.matchAll(/'([a-z_]+)'/g)].map((match) => match[1]);
}

describe("hotel detail vocabularies match the migration", () => {
  it("HOTEL_AMENITIES matches hotel_amenities_known_amenity", () => {
    expect([...HOTEL_AMENITIES].sort()).toEqual(
      checkConstraintValues("hotel_amenities_known_amenity").sort(),
    );
  });

  it("BED_CONFIGURATIONS matches room_types_bed_configuration_valid", () => {
    expect([...BED_CONFIGURATIONS].sort()).toEqual(
      checkConstraintValues("room_types_bed_configuration_valid").sort(),
    );
  });

  it("every amenity the migration allows has an Arabic label", async () => {
    const { AMENITY_LABELS } = await import("@/lib/hotelDetails");
    for (const amenity of checkConstraintValues("hotel_amenities_known_amenity")) {
      expect(AMENITY_LABELS[amenity as keyof typeof AMENITY_LABELS]).toBeTruthy();
    }
  });
});

describe("hotel location vocabularies match migration 0028", () => {
  it("HOTEL_CITIES matches hotels_city_valid", () => {
    expect([...HOTEL_CITIES].sort()).toEqual(
      checkConstraintValues("hotels_city_valid", locationMigrationSql).sort(),
    );
  });

  it("HOTEL_ZONES matches hotels_zone_valid", () => {
    expect([...HOTEL_ZONES].sort()).toEqual(
      checkConstraintValues("hotels_zone_valid", locationMigrationSql).sort(),
    );
  });

  it("every city and zone the migration allows has an Arabic label", () => {
    for (const city of checkConstraintValues("hotels_city_valid", locationMigrationSql)) {
      expect(CITY_LABELS[city as keyof typeof CITY_LABELS]).toBeTruthy();
    }
    for (const zone of checkConstraintValues("hotels_zone_valid", locationMigrationSql)) {
      expect(ZONE_LABELS[zone as keyof typeof ZONE_LABELS]).toBeTruthy();
    }
  });

  it("every zone name starts with one of the cities, as hotels_zone_matches_city expects", () => {
    for (const zone of HOTEL_ZONES) {
      expect(HOTEL_CITIES.some((city) => zone.startsWith(`${city}_`))).toBe(true);
    }
  });

  it("ISO_WEEKDAYS matches the array hotels_weekend_days_valid allows", () => {
    const match = /ARRAY\[([\d,\s]+)\]::smallint\[\]/.exec(locationMigrationSql);
    expect(match, "the weekday ARRAY[...] was not found").not.toBeNull();
    const allowed = (match?.[1] ?? "").split(",").map((day) => Number(day.trim()));
    expect([...ISO_WEEKDAYS]).toEqual(allowed);
    for (const day of ISO_WEEKDAYS) {
      expect(WEEKDAY_LABELS[day]).toBeTruthy();
    }
  });

  it("DEFAULT_WEEKEND_DAYS matches the column default", () => {
    const match = /weekend_days smallint\[\] NOT NULL DEFAULT '\{([\d,]+)\}'/.exec(
      locationMigrationSql,
    );
    expect(match, "the weekend_days DEFAULT was not found").not.toBeNull();
    const defaultDays = (match?.[1] ?? "").split(",").map(Number);
    expect([...DEFAULT_WEEKEND_DAYS]).toEqual(defaultDays);
  });

  it("MAX_DISTRICT_NAME_LENGTH matches hotels_district_name_valid", () => {
    const start = locationMigrationSql.indexOf("hotels_district_name_valid");
    expect(start).toBeGreaterThan(-1);
    const match = /BETWEEN 1 AND (\d+)/.exec(locationMigrationSql.slice(start));
    expect(match, "the district name BETWEEN bound was not found").not.toBeNull();
    expect(MAX_DISTRICT_NAME_LENGTH).toBe(Number(match?.[1]));
  });
});
