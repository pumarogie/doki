import logging
import signal
import time

from docforge import db
from docforge.core import jobs
from docforge.products import extract  # noqa: F401  (registers job handlers)

log = logging.getLogger("docforge.worker")


def main(poll_seconds: float = 1.0) -> None:
    logging.basicConfig(level=logging.INFO)
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    db.pool.open()
    log.info("worker started, handlers=%s", sorted(jobs.HANDLERS))
    try:
        while not stopping:
            if not jobs.run_once():
                time.sleep(poll_seconds)
    finally:
        db.pool.close()


if __name__ == "__main__":
    main()
