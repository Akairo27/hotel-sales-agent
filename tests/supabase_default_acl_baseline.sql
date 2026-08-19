-- The single source of truth for what a pristine Supabase project grants
-- anon/authenticated/service_role by default, before any migration in
-- db/migrations/ ever runs. Two consumers read this exact file, so it is
-- never hand-copied or restated from memory in either place:
--
--   1. tests/conftest.py's _schema fixture applies it to local Postgres
--      before applying db/migrations/, so every privilege/RLS test in
--      tests/integration/ runs against the same starting posture the
--      real database has, not a plain Postgres install that was never
--      vulnerable to this default in the first place.
--   2. scripts/verify_default_acl_baseline.py applies it to a set of
--      assertions it checks against a real, pristine, never-migrated
--      Supabase project's actual pg_default_acl/has_schema_privilege
--      state, so a change to Supabase's own provisioning template shows
--      up as a loud CI failure instead of a silent drift between what
--      this file assumes and what Supabase actually does.
--
-- Derived empirically this session, not from documentation, by querying
-- pg_default_acl on a freshly created Supabase project before any
-- migration ran (see db/migrations/0012_lock_default_privileges.sql's
-- and 0013's own comments for the individual findings this compiles).
-- Scoped to exactly what this repo's own migrations can be affected by:
-- role "postgres" (every migration here runs as that role) in schema
-- "public" (the only schema this repo's migrations ever create anything
-- in) — auth/storage/realtime/graphql/extensions are Supabase-managed
-- schemas owned by Supabase's own service roles, not postgres, and are
-- deliberately out of scope here.

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
GRANT ALL ON TABLES TO anon, authenticated, service_role;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
GRANT ALL ON SEQUENCES TO anon, authenticated, service_role;

-- Confirmed empirically this session: the automatic PUBLIC-gets-EXECUTE
-- default Postgres applies to every new function is suppressed the
-- moment ANY default-ACL row exists for (postgres, public) — even one
-- that only concerns TABLES, not functions. A pristine Supabase project
-- has its own explicit functions row here (not just tables/sequences),
-- so this line is required for an accurate simulation, not redundant
-- with the automatic PUBLIC grant — tested directly: omitting it made a
-- freshly created function NOT executable by a role holding only the
-- table/sequence defaults above.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
GRANT EXECUTE ON FUNCTIONS TO anon, authenticated, service_role;

-- service_role and authenticated both get explicit schema USAGE from
-- this repo's own migrations (0001, 0010 respectively) — identical in
-- both environments, so not simulated here. anon never gets schema
-- USAGE from any migration in this repo, yet a real Supabase project
-- grants it by default regardless — confirmed via has_schema_privilege
-- on a real project. Without this, anon fails one step earlier
-- (UndefinedTable/UndefinedFunction, name resolution) than it does on
-- real Supabase (InsufficientPrivilege, a real permission check) — the
-- gap this whole file exists to close.
GRANT USAGE ON SCHEMA public TO anon;
