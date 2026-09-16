-- Per-key abuse limits. monthly_pages NULL = unlimited.
ALTER TABLE api_key ADD COLUMN rate_per_minute int NOT NULL DEFAULT 60 CHECK (rate_per_minute > 0);
ALTER TABLE api_key ADD COLUMN monthly_pages int CHECK (monthly_pages > 0);

-- Fixed one-minute request windows; the worker prunes rows older than an hour.
CREATE TABLE rate_window (
    api_key_id   bigint NOT NULL REFERENCES api_key ON DELETE RESTRICT,
    window_start timestamptz NOT NULL,
    hits         int NOT NULL DEFAULT 0,
    PRIMARY KEY (api_key_id, window_start)
);
CREATE INDEX rate_window_start_idx ON rate_window (window_start);
