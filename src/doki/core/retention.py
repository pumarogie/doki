"""Deletes old data so disk and tables stop growing. Run by the worker once an hour."""
import logging
import os

from doki.core import storage
from doki.db import connect

log = logging.getLogger(__name__)

RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
USAGE_RETENTION_MONTHS = int(os.environ.get("USAGE_RETENTION_MONTHS", "13"))  # keep a full billing year


def purge(retention_days: int = RETENTION_DAYS, usage_months: int = USAGE_RETENTION_MONTHS) -> dict:
    with connect() as conn:
        # Idempotency replays only need a day; they reference stored responses, so they go first.
        conn.execute("DELETE FROM idempotency_key WHERE created_at < now() - interval '24 hours'")
        jobs = conn.execute(
            """DELETE FROM job WHERE status IN ('done', 'failed')
               AND finished_at < now() - make_interval(days => %s)""",
            (retention_days,),
        ).rowcount
        # Files still used by a remaining (queued/running/recent) job stay.
        shas = [r["sha256"] for r in conn.execute(
            """DELETE FROM file f WHERE f.created_at < now() - make_interval(days => %s)
               AND NOT EXISTS (SELECT 1 FROM job j WHERE j.input_file_id = f.id OR j.output_file_id = f.id)
               AND NOT EXISTS (SELECT 1 FROM idempotency_key i WHERE i.response_file_id = f.id)
               RETURNING f.sha256""",
            (retention_days,),
        )]
        usage = conn.execute(
            "DELETE FROM usage_event WHERE created_at < date_trunc('month', now()) - make_interval(months => %s)",
            (usage_months,),
        ).rowcount
    blobs = sum(storage.delete_blob_if_unreferenced(sha) for sha in set(shas))
    result = {"jobs": jobs, "files": len(shas), "blobs": blobs, "usage_events": usage}
    if any(result.values()):
        log.info("retention purge %s", result)
    return result
