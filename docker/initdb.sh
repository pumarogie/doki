#!/bin/sh
# Runs once when the Postgres volume is created. The app never connects as a superuser:
# doki_owner owns the schema (migrations), doki_app only reads and writes rows.
set -eu
DB="${DOKI_DB:-doki}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v owner_pw="$DOKI_OWNER_PASSWORD" -v app_pw="$DOKI_APP_PASSWORD" -v db="$DB" <<'SQL'
CREATE ROLE doki_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE doki_app LOGIN PASSWORD :'app_pw';
ALTER ROLE doki_app SET statement_timeout = '30s';
ALTER ROLE doki_app SET lock_timeout = '5s';
ALTER ROLE doki_app SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE doki_owner SET lock_timeout = '10s';
CREATE DATABASE :"db" OWNER doki_owner;
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO doki_app;
SQL
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$DB" <<'SQL'
ALTER SCHEMA public OWNER TO doki_owner;
GRANT USAGE ON SCHEMA public TO doki_app;
ALTER DEFAULT PRIVILEGES FOR ROLE doki_owner GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO doki_app;
ALTER DEFAULT PRIVILEGES FOR ROLE doki_owner GRANT USAGE, SELECT ON SEQUENCES TO doki_app;
SQL
