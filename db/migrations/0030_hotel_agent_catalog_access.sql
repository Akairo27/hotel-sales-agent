-- hotel_agent gains read access to hotels and room_types — the still-missing
-- piece behind an incident where the model, asked for a hotel by name, had
-- no way to resolve it to a real id and guessed. search_hotels
-- (services/agent/llm/tools.py, dispatch.py) is that resolution path; this
-- migration is its only schema dependency.
--
-- Full-table GRANT SELECT, not column-scoped: matches migration 0027's own
-- established pattern for every other reference table this role reads
-- (allotments, room_night_inventory, seasons, price_rules,
-- price_overrides) -- the boundary against a sensitive column reaching the
-- model is enforced by what dispatch.py's hand-built result dict contains,
-- not by withholding a DB grant (see 0027's own comment on cost_per_night
-- for the identical reasoning). Neither table has a cost column at all, so
-- this is if anything a simpler case than that precedent already covers.
-- address_text is included: dispatch_search_hotels needs it to compute
-- profile completeness (services/agent/hotel_profile.py) even though it is
-- never placed in a tool result.

GRANT SELECT ON TABLE hotels TO hotel_agent;
GRANT SELECT ON TABLE room_types TO hotel_agent;

CREATE POLICY hotels_agent_select ON hotels
FOR SELECT TO hotel_agent
USING (true);

CREATE POLICY room_types_agent_select ON room_types
FOR SELECT TO hotel_agent
USING (true);
