-- Replays a completed POST for the same (api key, Idempotency-Key). Rows expire after 24 hours.
CREATE TABLE idempotency_key (
    api_key_id       bigint NOT NULL REFERENCES api_key ON DELETE RESTRICT,
    key              text NOT NULL CHECK (length(key) BETWEEN 1 AND 255),
    request_hash     text NOT NULL,
    status_code      int,
    content_type     text,
    response_file_id bigint REFERENCES file ON DELETE RESTRICT,
    created_at       timestamptz NOT NULL DEFAULT now(),
    completed_at     timestamptz,
    PRIMARY KEY (api_key_id, key)
);
CREATE INDEX idempotency_key_created_idx ON idempotency_key (created_at);
CREATE INDEX idempotency_key_response_file_idx ON idempotency_key (response_file_id);
