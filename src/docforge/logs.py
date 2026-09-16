"""JSON logs with a request/job id on every line."""
import contextvars
import json
import logging
import os
import re
import time
import uuid
from datetime import UTC, datetime

request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_STD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id.get(),
        }
        entry.update({k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS and not k.startswith("_")})
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure() -> None:
    handler = logging.StreamHandler()
    if os.environ.get("LOG_FORMAT", "json") == "json":
        handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def new_id(incoming: str | None = None) -> str:
    return incoming if incoming and _VALID_ID.match(incoming) else uuid.uuid4().hex


class RequestIdMiddleware:
    """Pure ASGI (not BaseHTTPMiddleware) so the id stays in the same context as exception handlers."""

    def __init__(self, app) -> None:
        self.app = app
        self.log = logging.getLogger("docforge.access")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        rid = new_id(headers.get(b"x-request-id", b"").decode("latin-1") or None)
        request_id.set(rid)
        started = time.perf_counter()
        status = 500

        async def send_with_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], (b"x-request-id", rid.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            self.log.info(
                "%s %s %s", scope["method"], scope["path"], status,
                extra={"method": scope["method"], "path": scope["path"], "status": status,
                       "duration_ms": round((time.perf_counter() - started) * 1000, 1)},
            )
