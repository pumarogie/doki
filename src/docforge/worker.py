import logging
import os
import signal
import time

from docforge import (
    db,
    logs,
    observability,
    webhooks,  # noqa: F401  (registers job handlers)
)
from docforge.core import jobs, retention
from docforge.products import extract, reports  # noqa: F401  (registers job handlers)

log = logging.getLogger("docforge.worker")


def main(poll_seconds: float = 1.0, schedule_every: float = 20.0, purge_every: float = 3600.0) -> None:
    logs.configure()
    observability.init_sentry("worker")
    if port := os.environ.get("WORKER_METRICS_PORT"):
        from prometheus_client import start_http_server

        start_http_server(int(port))
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    db.pool.open()
    log.info("worker started, handlers=%s", sorted(jobs.HANDLERS))
    last_schedule_check = 0.0
    last_purge = 0.0
    try:
        while not stopping:
            if time.monotonic() - last_schedule_check >= schedule_every:
                if queued := reports.enqueue_due():
                    log.info("queued %d scheduled report(s)", queued)
                last_schedule_check = time.monotonic()
            if time.monotonic() - last_purge >= purge_every:
                retention.purge()
                last_purge = time.monotonic()
            if not jobs.run_once():
                time.sleep(poll_seconds)
    finally:
        db.pool.close()


if __name__ == "__main__":
    main()
