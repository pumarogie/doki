#!/bin/sh
# Runs once when the Postgres volume is created. The app never connects as a superuser:
# docforge_owner owns the schema (migrations), docforge_app only reads and writes rows.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v owner_pw="$DOCFORGE_OWNER_PASSWORD" -v app_pw="$DOCFORGE_APP_PASSWORD" <<'SQL'
CREATE ROLE docforge_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE docforge_app LOGIN PASSWORD :'app_pw';
ALTER ROLE docforge_app SET statement_timeout = '30s';
ALTER ROLE docforge_app SET lock_timeout = '5s';
ALTER ROLE docforge_app SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE docforge_owner SET lock_timeout = '10s';
CREATE DATABASE docforge OWNER docforge_owner;
REVOKE ALL ON DATABASE docforge FROM PUBLIC;
GRANT CONNECT ON DATABASE docforge TO docforge_app;
SQL
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname docforge <<'SQL'
ALTER SCHEMA public OWNER TO docforge_owner;
GRANT USAGE ON SCHEMA public TO docforge_app;
ALTER DEFAULT PRIVILEGES FOR ROLE docforge_owner GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO docforge_app;
ALTER DEFAULT PRIVILEGES FOR ROLE docforge_owner GRANT USAGE, SELECT ON SEQUENCES TO docforge_app;
SQL
