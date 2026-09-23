-- The migration runner creates schema_migrations on the fly without row-level security, which
-- Supabase's Security Advisor flags: the public anon key could read or change the list of applied
-- migrations. No policies are needed; the runner connects directly as the database owner.
alter table if exists schema_migrations enable row level security;
