// Riyal <-> halala conversion for the allotment entry screen — the one
// place in the admin UI where staff type a money amount rather than only
// display one. CLAUDE.md rule 5: all money is stored as an integer count
// of halalas (`1250` means 12.50 SAR); this module is the only boundary
// where that integer is derived from or reduced to a whole-riyal amount a
// person typed, mirroring lib/money.py's role as the one money-display
// helper on the backend.
//
// Staff enter cost per night in whole riyals only (owner decision,
// 2026-09-28) — no fractional riyals, so the conversion is exact in both
// directions with no rounding: admin_set_allotments only ever receives a
// halalas value this module produced from an integer riyals input, and
// only ever displays one it produced back from that same kind of value.

const HALALAS_PER_SAR = 100;

/** Converts a whole-riyal amount a staff member typed into the halalas
 * integer admin_set_allotments expects.
 *
 * Raises:
 *   RangeError: riyals is not a non-negative integer — this system has no
 *     path that needs a negative or fractional riyal amount.
 */
export function riyalsToHalalas(riyals: number): number {
  if (!Number.isInteger(riyals) || riyals < 0) {
    throw new RangeError(`riyals must be a non-negative integer, got ${riyals}`);
  }
  return riyals * HALALAS_PER_SAR;
}

/** Converts a halalas amount (e.g. an existing allotment's cost_per_night)
 * back into the whole riyals the entry form displays.
 *
 * Raises:
 *   RangeError: halalas is not a non-negative exact multiple of 100 — every
 *     value this screen ever wrote is one, by construction; anything else
 *     is not a value this form can round-trip without silently changing it.
 */
export function halalasToRiyals(halalas: number): number {
  if (!Number.isInteger(halalas) || halalas < 0 || halalas % HALALAS_PER_SAR !== 0) {
    throw new RangeError(`halalas must be a non-negative exact multiple of 100, got ${halalas}`);
  }
  return halalas / HALALAS_PER_SAR;
}
