-- Links billed work to the trace id stamped into generated PDFs. Lookups are rare (leak investigations)
-- and can be bounded by time via the BRIN index on created_at, so no extra index.
ALTER TABLE usage_event ADD COLUMN request_id text;
