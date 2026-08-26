// seasons carries no color column (db/migrations/0002_seasons.sql) — colors
// are assigned deterministically from a season's id rather than stored, so
// the same season always renders the same color without a schema change.
// Tuned for the dashboard's near-black surface (--background in
// admin/app/globals.css): the mid-tone 600-level versions of these hues
// that a light theme would use drop to roughly 2:1 against it, which on a
// 14px calendar cell is indistinguishable from an empty day.
const PALETTE = [
  "#60a5fa", // blue
  "#f87171", // red
  "#4ade80", // green
  "#fbbf24", // amber
  "#c084fc", // purple
  "#22d3ee", // cyan
  "#f472b6", // pink
  "#a3e635", // lime
  "#818cf8", // indigo
  "#fb923c", // orange
] as const;

export function seasonColor(seasonId: number): string {
  const index = ((seasonId % PALETTE.length) + PALETTE.length) % PALETTE.length;
  return PALETTE[index];
}

// The default season fills every gap, so this has to read as "covered,
// but by nothing in particular" — present, and quieter than every hue in
// PALETTE rather than brighter than all of them.
export const GAP_COLOR = "#4b5158";
