CREATE TABLE organization (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE api_key (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    organization_id bigint NOT NULL REFERENCES organization ON DELETE RESTRICT,
    prefix          text NOT NULL UNIQUE,
    key_hash        text NOT NULL UNIQUE,
    created_at      timestamptz NOT NULL DEFAULT now(),
    revoked_at      timestamptz
);
CREATE INDEX api_key_organization_idx ON api_key (organization_id);

CREATE TABLE api_key_product (
    api_key_id bigint NOT NULL REFERENCES api_key ON DELETE RESTRICT,
    product    text NOT NULL CHECK (product IN ('pdfa', 'einvoice', 'render', 'extract', 'reports')),
    PRIMARY KEY (api_key_id, product)
);

CREATE TABLE file (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    organization_id bigint NOT NULL REFERENCES organization ON DELETE RESTRICT,
    sha256          text NOT NULL,
    filename        text NOT NULL,
    content_type    text NOT NULL,
    size_bytes      bigint NOT NULL CHECK (size_bytes >= 0),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, sha256)
);
CREATE INDEX file_organization_created_idx ON file (organization_id, created_at DESC);

CREATE TABLE template (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    organization_id bigint NOT NULL REFERENCES organization ON DELETE RESTRICT,
    name            text NOT NULL,
    engine          text NOT NULL CHECK (engine IN ('typst', 'html')),
    source          text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organization_id, name)
);

CREATE TABLE schedule (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    organization_id bigint NOT NULL REFERENCES organization ON DELETE RESTRICT,
    template_id     bigint NOT NULL REFERENCES template ON DELETE RESTRICT,
    cron            text NOT NULL,
    data            jsonb NOT NULL DEFAULT '{}',
    delivery        jsonb NOT NULL,
    next_run_at     timestamptz NOT NULL,
    enabled         boolean NOT NULL DEFAULT true
);
CREATE INDEX schedule_template_idx ON schedule (template_id);
CREATE INDEX schedule_organization_idx ON schedule (organization_id);
CREATE INDEX schedule_due_idx ON schedule (next_run_at) WHERE enabled;

CREATE TABLE job (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    api_key_id     bigint NOT NULL REFERENCES api_key ON DELETE RESTRICT,
    product        text NOT NULL,
    operation      text NOT NULL,
    status         text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
    params         jsonb NOT NULL DEFAULT '{}',
    input_file_id  bigint REFERENCES file ON DELETE RESTRICT,
    output_file_id bigint REFERENCES file ON DELETE RESTRICT,
    result         jsonb,
    error          text,
    attempts       int NOT NULL DEFAULT 0,
    run_after      timestamptz NOT NULL DEFAULT now(),
    created_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz
);
CREATE INDEX job_queued_idx ON job (run_after) WHERE status = 'queued';
CREATE INDEX job_api_key_created_idx ON job (api_key_id, created_at DESC);
CREATE INDEX job_input_file_idx ON job (input_file_id);
CREATE INDEX job_output_file_idx ON job (output_file_id);

CREATE TABLE usage_event (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    api_key_id bigint NOT NULL REFERENCES api_key ON DELETE RESTRICT,
    product    text NOT NULL,
    operation  text NOT NULL,
    page_count int NOT NULL DEFAULT 0 CHECK (page_count >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX usage_event_api_key_created_idx ON usage_event (api_key_id, created_at);
CREATE INDEX usage_event_created_brin ON usage_event USING brin (created_at);
