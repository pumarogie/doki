-- Token-bucket (GCRA) rate limiting: one row per key holding the theoretical arrival time of the
-- next request. Replaces fixed minute windows, which allowed 2x bursts across a minute boundary.
DROP TABLE rate_window;
CREATE TABLE rate_limit_state (
    api_key_id bigint PRIMARY KEY REFERENCES api_key ON DELETE RESTRICT,
    tat        timestamptz NOT NULL
);
