// Pins admin/lib/hotelDetails.ts's closed lists to the CHECK constraints in
// db/migrations/0023_hotel_details.sql that actually enforce them.
//
// Same purpose as seasonCalendar.conformance.test.ts, by a different route:
// there is no generated fixture here because the ground truth is plain SQL,
// so the migration is read and its IN (...) lists parsed directly. Adding an
// amenity to one side and not the other fails this test rather than showing
// up as a save that is rejected by Postgres with an English error.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { BED_CONFIGURATIONS, HOTEL_AMENITIES } from "@/lib/hotelDetails";

const MIGRATION_PATH = fileURLToPath(
  new URL("../../db/migrations/0023_hotel_details.sql", import.meta.url),
);

const migrationSql = readFileSync(MIGRATION_PATH, "utf8");

/** Returns the quoted values of the `IN (...)` list attached to a named
 * CHECK constraint, in the order the migration writes them. */
function checkConstraintValues(constraintName: string): string[] {
  const constraintIndex = migrationSql.indexOf(constraintName);
  expect(
    constraintIndex,
    `constraint ${constraintName} not found in the migration`,
  ).toBeGreaterThan(-1);

  const inIndex = migrationSql.indexOf("IN (", constraintIndex);
  expect(inIndex, `no IN (...) list after ${constraintName}`).toBeGreaterThan(-1);

  const closingIndex = migrationSql.indexOf(")", inIndex + "IN (".length);
  const list = migrationSql.slice(inIndex + "IN (".length, closingIndex);
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
