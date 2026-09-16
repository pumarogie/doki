import json
import logging

from fastapi.testclient import TestClient

from docforge import logs
from docforge.main import app


@app.get("/_test/boom")
def _boom():
    raise RuntimeError("secret internal detail")


def test_response_carries_generated_request_id(client):
    r = client.get("/health")
    assert len(r.headers["x-request-id"]) == 32


def test_incoming_request_id_is_echoed(client):
    assert client.get("/health", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"] == "abc-123"


def test_unsafe_request_id_is_replaced(client):
    rid = client.get("/health", headers={"X-Request-ID": "bad id\nwith newline"}).headers["x-request-id"]
    assert rid != "bad id\nwith newline" and len(rid) == 32


def test_500_body_and_header_share_the_id_without_leaking(client):
    # No `with`: running the lifespan again would close the shared DB pool.
    r = TestClient(app, raise_server_exceptions=False).get("/_test/boom", headers={"X-Request-ID": "trace-500"})
    assert r.status_code == 500
    assert r.json() == {"error": "internal_error", "request_id": "trace-500"}
    assert r.headers["x-request-id"] == "trace-500"
    assert "secret" not in r.text


def test_access_log_line_has_request_fields(client, caplog):
    with caplog.at_level(logging.INFO, logger="docforge.access"):
        client.get("/health", headers={"X-Request-ID": "log-me"})
    record = next(r for r in caplog.records if r.name == "docforge.access")
    line = json.loads(logs.JsonFormatter().format(record))
    assert line["path"] == "/health" and line["status"] == 200 and line["method"] == "GET"
    assert line["duration_ms"] >= 0


def test_json_formatter_includes_context_id():
    token = logs.request_id.set("job-42")
    try:
        record = logging.makeLogRecord({"name": "x", "levelname": "INFO", "msg": "hello %s", "args": ("world",)})
        line = json.loads(logs.JsonFormatter().format(record))
    finally:
        logs.request_id.reset(token)
    assert line["msg"] == "hello world" and line["request_id"] == "job-42"
