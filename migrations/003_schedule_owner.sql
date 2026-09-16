-- Scheduled runs create jobs, and every job belongs to an API key. The owning key replaces
-- organization_id (reachable through the key); revoking the key stops its schedules.
ALTER TABLE schedule DROP COLUMN organization_id;
ALTER TABLE schedule ADD COLUMN api_key_id bigint NOT NULL REFERENCES api_key ON DELETE RESTRICT;
ALTER TABLE schedule ADD COLUMN name text NOT NULL;
ALTER TABLE schedule ADD COLUMN timezone text NOT NULL DEFAULT 'UTC';
CREATE INDEX schedule_api_key_idx ON schedule (api_key_id);
