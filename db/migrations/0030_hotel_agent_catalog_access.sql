-- hotel_agent gains read access to hotels and room_types — the still-missing
-- piece behind an incident where the model, asked for a hotel by name, had
-- no way to resolve it to a real id and guessed. search_hotels
-- (services/agent/llm/tools.py, dispatch.py) is that resolution path; this
-- migration is its only schema dependency.
--
-- Column-scoped, not full-table: hotels is expected to grow columns this
-- role must never see automatically just by being added (a per-hotel
-- FAREAST meal price, contract or supplier contact details) -- a
-- full-table grant would expose each one to hotel_agent the moment it
-- exists, with no further review. Every column below is exactly one
-- search_hotels (services/agent/llm/dispatch.py) itself reads: the eight
-- it can place in a result, plus address_text and is_active for the
-- completeness/active filter (address_text is never placed in a result).

GRANT SELECT (
    id, hotel_name, city, zone, district_name, star_rating,
    distance_to_haram_meters, address_text, is_active
) ON TABLE hotels TO hotel_agent;

GRANT SELECT (
    id, hotel_id, room_type_name, capacity_adults, bed_configuration
) ON TABLE room_types TO hotel_agent;

CREATE POLICY hotels_agent_select ON hotels
FOR SELECT TO hotel_agent
USING (true);

CREATE POLICY room_types_agent_select ON room_types
FOR SELECT TO hotel_agent
USING (true);
