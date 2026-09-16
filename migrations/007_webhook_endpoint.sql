-- Customer URLs that receive signed job.succeeded / job.failed events.
CREATE TABLE webhook_endpoint (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    organization_id bigint NOT NULL REFERENCES organization ON DELETE RESTRICT,
    url             text NOT NULL CHECK (length(url) <= 2000),
    secret_enc      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX webhook_endpoint_organization_idx ON webhook_endpoint (organization_id);
