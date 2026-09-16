-- Support the retention purge: old files by age, finished jobs by completion time.
CREATE INDEX file_created_idx ON file (created_at);
CREATE INDEX job_finished_idx ON job (finished_at) WHERE status IN ('done', 'failed');
