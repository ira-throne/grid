-- Optional for running on your laptop; do this before the site is public.
-- A login that can only read the grid schema, for the web app. If the
-- dashboard ever had a bug (or someone found a way to abuse it), the
-- worst it could do is read data that is already public.
--
-- Run as your normal (superuser) login:
--   psql -d grid -v pw="'pick-a-password'" -f sql/05_dashboard_role.sql
-- then add to .env:
--   DASHBOARD_DATABASE_URL=postgresql://grid_reader:pick-a-password@localhost:5432/grid

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grid_reader') THEN
        CREATE ROLE grid_reader LOGIN;
    END IF;
END $$;

ALTER ROLE grid_reader PASSWORD :pw;

GRANT CONNECT ON DATABASE grid TO grid_reader;
GRANT USAGE ON SCHEMA grid TO grid_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA grid TO grid_reader;   -- includes the views
-- Tables and views created later get the same read access automatically.
ALTER DEFAULT PRIVILEGES IN SCHEMA grid GRANT SELECT ON TABLES TO grid_reader;
-- No access to the raw schema at all: the dashboard never needs it.
