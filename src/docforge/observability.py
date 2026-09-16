"""Error tracking (Sentry, opt-in via SENTRY_DSN) and Prometheus metrics."""
import os
import secrets as pysecrets
import time

from fastapi import APIRouter, Header, HTTPException, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

HTTP_REQUESTS = Counter("docforge_http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_SECONDS = Histogram("docforge_http_request_seconds", "HTTP request duration", ["route"],
                         buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30))
JOBS = Counter("docforge_jobs_total", "Finished job attempts", ["product", "operation", "outcome"])
JOB_SECONDS = Histogram("docforge_job_seconds", "Job run time", ["product", "operation"],
                        buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300))
TOOL_SECONDS = Histogram("docforge_tool_seconds", "External engine call time", ["tool"],
                         buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30))
QUEUE_DEPTH = Gauge("docforge_jobs_queued", "Jobs waiting to run")

def init_sentry(component: str) -> bool:
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return False
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        release=os.environ.get("SENTRY_RELEASE"),
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0")),
        send_default_pii=False,
        max_request_body_size="never",
        include_local_variables=False,
        server_name=component,
    )
    return True


def refresh_queue_depth() -> None:
    from docforge.db import connect, one

    with connect() as conn:
        QUEUE_DEPTH.set(one(conn, "SELECT count(*) AS n FROM job WHERE status = 'queued'")["n"])


router = APIRouter(include_in_schema=False)


@router.get("/metrics")
def metrics(authorization: str | None = Header(default=None)):
    token = os.environ.get("METRICS_TOKEN")
    if not token:
        raise HTTPException(404)  # disabled unless a scrape token is configured
    if not authorization or not pysecrets.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(401)
    refresh_queue_depth()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


class MetricsMiddleware:
    """Counts requests by route template (not raw path) to keep label cardinality bounded."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def capture(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            if route != "/metrics":
                HTTP_REQUESTS.labels(scope["method"], route, str(status)).inc()
                HTTP_SECONDS.labels(route).observe(time.perf_counter() - started)
