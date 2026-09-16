import logging
import time
from collections.abc import Callable

from psycopg.types.json import Jsonb

from docforge import logs
from docforge.db import connect, one

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
STALE_AFTER = "10 minutes"


class PermanentError(Exception):
    """Retrying won't help. The message is shown to the client."""


# (product, operation) -> handler(job) -> result dict.
# A result key "output_file_id" is moved into job.output_file_id.
Handler = Callable[[dict], dict]
HANDLERS: dict[tuple[str, str], Handler] = {}


def handler(product: str, operation: str):
    def register(fn: Handler) -> Handler:
        HANDLERS[(product, operation)] = fn
        return fn
    return register


def enqueue(api_key_id: int, product: str, operation: str, params: dict, input_file_id: int | None = None) -> int:
    with connect() as conn:
        return one(
            conn,
            """INSERT INTO job (api_key_id, product, operation, params, input_file_id)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (api_key_id, product, operation, Jsonb(params), input_file_id),
        )["id"]


def claim() -> dict | None:
    with connect() as conn:
        # Requeue jobs whose worker died mid-run.
        conn.execute(
            f"UPDATE job SET status = 'queued', started_at = NULL WHERE status = 'running' AND started_at < now() - interval '{STALE_AFTER}'"
        )
        return conn.execute(
            """UPDATE job SET status = 'running', started_at = now(), attempts = attempts + 1
               WHERE id = (SELECT id FROM job WHERE status = 'queued' AND run_after <= now()
                           ORDER BY run_after FOR UPDATE SKIP LOCKED LIMIT 1)
               RETURNING id, api_key_id, product, operation, params, input_file_id, attempts""",
        ).fetchone()


def finish(job_id: int, result: dict, output_file_id: int | None = None) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE job SET status = 'done', result = %s, output_file_id = %s, error = NULL, finished_at = now()
               WHERE id = %s""",
            (Jsonb(result), output_file_id, job_id),
        )


def fail(job: dict, message: str, permanent: bool) -> None:
    with connect() as conn:
        if permanent or job["attempts"] >= MAX_ATTEMPTS:
            conn.execute(
                "UPDATE job SET status = 'failed', error = %s, finished_at = now() WHERE id = %s",
                (message, job["id"]),
            )
        else:
            conn.execute(
                """UPDATE job SET status = 'queued', started_at = NULL,
                          run_after = now() + make_interval(secs => 30 * power(2, attempts - 1))
                   WHERE id = %s""",
                (job["id"],),
            )


def run_once() -> bool:
    """Claim and run one job. Returns False when the queue is empty."""
    job = claim()
    if job is None:
        return False
    fn = HANDLERS.get((job["product"], job["operation"]))
    token = logs.request_id.set(f"job-{job['id']}")
    started = time.perf_counter()
    outcome = "done"
    try:
        if fn is None:
            raise PermanentError("unsupported operation")
        result = fn(job)
    except PermanentError as e:
        outcome = "failed"
        fail(job, str(e), permanent=True)
    except Exception:
        outcome = "retry" if job["attempts"] < MAX_ATTEMPTS else "failed"
        # Details go to logs only; clients see a generic message.
        log.exception("job %s failed (attempt %s)", job["id"], job["attempts"])
        fail(job, "processing failed", permanent=False)
    else:
        finish(job["id"], result, output_file_id=result.pop("output_file_id", None))
    finally:
        from docforge.observability import JOB_SECONDS, JOBS

        JOBS.labels(job["product"], job["operation"], outcome).inc()
        JOB_SECONDS.labels(job["product"], job["operation"]).observe(time.perf_counter() - started)
        logs.request_id.reset(token)
    return True


def get(job_id: int, organization_id: int) -> dict | None:
    with connect() as conn:
        return conn.execute(
            """SELECT j.id, j.product, j.operation, j.status, j.result, j.error, j.attempts,
                      j.output_file_id, j.created_at, j.finished_at
               FROM job j JOIN api_key k ON k.id = j.api_key_id
               WHERE j.id = %s AND k.organization_id = %s""",
            (job_id, organization_id),
        ).fetchone()
