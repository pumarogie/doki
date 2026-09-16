-- Lets the worker requeue jobs whose worker died mid-run.
ALTER TABLE job ADD COLUMN started_at timestamptz;
CREATE INDEX job_running_started_idx ON job (started_at) WHERE status = 'running';
