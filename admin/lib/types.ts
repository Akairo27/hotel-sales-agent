// Mirrors db/migrations pending 0010_app_users_and_roles.sql (see
// docs/phase-3-pr-a/README.md). Hand-maintained until `supabase gen types
// typescript` is run against the applied schema — that command needs a
// linked Supabase project and network access neither of which this
// session's permissions allow, so this is not yet auto-generated.
import type { BedConfiguration, HotelAmenity } from "@/lib/hotelDetails";

export type AppRole = "admin" | "sales";

export interface AppUser {
  id: string;
  full_name: string;
  app_role: AppRole;
  can_view_cost: boolean;
  is_active: boolean;
  created_at: string;
}

// Mirrors db/migrations/0001_hotels_room_types.sql plus the detail columns
// 0023_hotel_details.sql adds. Neither table carries a cost column, so
// unlike AppUser there is no ARCHITECTURE.md §8 masking concern here.
//
// Every detail column is nullable on purpose: 0023 ran against rows that
// predated it and there is no honest default for a real hotel's distance
// from the Haram, so "not recorded yet" is a state the UI has to render
// rather than a case it can assume away. admin/lib/hotelDetails.ts's
// missingHotelProfileFields is what surfaces it.
// The three columns every screen other than the hotel profile selects: an
// id to key on, a name to render, and created_at. Kept as its own type
// because overrideTypes<T> casts the result without checking it — typing a
// three-column query as the full Hotel would be a silent lie about fields
// that are not in the payload, not a compile error.
export interface HotelRef {
  id: number;
  hotel_name: string;
  created_at: string;
}

export interface Hotel extends HotelRef {
  distance_to_haram_meters: number | null;
  star_rating: number | null;
  address_text: string | null;
  check_in_time: string | null;
  check_out_time: string | null;
  is_active: boolean;
}

export interface RoomTypeRef {
  id: number;
  hotel_id: number;
  room_type_name: string;
  created_at: string;
}

export interface RoomType extends RoomTypeRef {
  capacity_adults: number | null;
  size_sqm: number | null;
  bed_configuration: BedConfiguration | null;
}

// One row per amenity a hotel has; absence is what "does not have it"
// means. The closed list lives in admin/lib/hotelDetails.ts, pinned to the
// migration's CHECK by hotelDetails.conformance.test.ts.
export interface HotelAmenityRow {
  hotel_id: number;
  amenity: HotelAmenity;
  created_at: string;
}

// Mirrors db/migrations/0002_seasons.sql. resolve_season_id
// (services/pricing/seasons.py) ignores a default row's own start/end
// entirely and falls back to it whenever no non-default season matches —
// its bounds exist only to satisfy the NOT NULL columns, never rendered.
export type CalendarType = "hijri" | "gregorian";

export interface Season {
  id: number;
  season_name: string;
  calendar_type: CalendarType;
  start_month: number;
  start_day: number;
  end_month: number;
  end_day: number;
  priority: number;
  is_default: boolean;
  created_at: string;
}

// Mirrors db/migrations/0016_allotments_cost_masking.sql's
// allotments_for_dashboard VIEW, not the allotments table itself —
// cost_per_night is null whenever the querying user's can_view_cost is
// false (ARCHITECTURE.md §8), so this type reflects that directly instead
// of pretending the column is always present.
export interface AllotmentForDashboard {
  id: number;
  hotel_id: number;
  room_type_id: number;
  stay_date: string;
  total_rooms: number;
  cost_per_night: number | null;
  created_at: string;
}

// Mirrors db/migrations/0006_price_rules.sql's jsonb band shapes, and the
// validation admin/lib/priceRuleBands.ts enforces client-side against the
// same shapes (see that module's own comment for the split between what
// Postgres's CHECK constraints guarantee and what this port additionally
// diagnoses). A band's max is null only in a lead-time chain's terminal,
// open-ended band — never in an occupancy chain, which is closed at 1.
export interface MinProfitBand {
  min_lead_days: number;
  max_lead_days: number | null;
  min_profit_halalas: number;
}

export interface MinProfitByLeadTime {
  bands: MinProfitBand[];
}

export interface DemandLeadTimeBand {
  min_lead_days: number;
  max_lead_days: number | null;
  multiplier_bps: number;
}

export interface OccupancyBand {
  min: number;
  max: number;
  multiplier_bps: number;
}

export interface DemandCurve {
  occupancy_bands: OccupancyBand[];
  lead_time_bands: DemandLeadTimeBand[];
}

export type PriceRuleScope = "global" | "season" | "hotel" | "room_type";

// Mirrors db/migrations/0018_price_rules_admin_access.sql's (and 0020's)
// price_rules_for_dashboard VIEW, not the price_rules table itself —
// target_margin_bps and min_profit_by_lead_time are null whenever the
// querying user's can_view_cost is false, the same masking shape as
// AllotmentForDashboard above. demand_curve carries no cost signal and is
// never masked, but it is still a field-by-field inheritance column like
// the two masked ones (ARCHITECTURE.md §5) — null on a non-global row
// means "not overridden at this scope", not "no value". Only the global
// row's price_rules_global_is_complete CHECK guarantees it non-null there.
export interface PriceRuleForDashboard {
  id: number;
  scope: PriceRuleScope;
  scope_id: number | null;
  demand_curve: DemandCurve | null;
  created_at: string;
  is_active: boolean;
  target_margin_bps: number | null;
  min_profit_by_lead_time: MinProfitByLeadTime | null;
}

// Mirrors db/migrations/0007_price_overrides.sql's price_overrides table
// directly — unlike PriceRuleForDashboard/AllotmentForDashboard, there is
// no masking VIEW here (db/migrations/0021_price_overrides_admin_access.sql's
// own comment: none of these three columns reverse-derive cost, so none go
// behind can_view_cost). "Ended early" is not a separate flag — it is
// expires_at set to now or earlier; services/pricing/compute.py's
// _fetch_active_override already excludes any row where expires_at is not
// in the future.
export interface PriceOverride {
  id: number;
  hotel_id: number;
  room_type_id: number;
  stay_date: string;
  ask_price_override: number;
  min_allowed_override: number;
  expires_at: string;
  created_at: string;
}
