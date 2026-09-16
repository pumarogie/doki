import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from docforge import webhooks
from docforge.core import jobs
from docforge.db import connect, one


class Receiver:
    def __init__(self):
        self.requests: list[dict] = []
        self.status = 200
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                receiver.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                self.send_response(receiver.status)
                if receiver.status == 302:
                    self.send_header("Location", "http://169.254.169.254/latest/meta-data")
                self.end_headers()

            def log_message(self, format, *args):  # silence request logs
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def receiver():
    r = Receiver()
    yield r
    r.server.shutdown()


@pytest.fixture(autouse=True)
def empty_queue(client):
    while jobs.run_once():
        pass


def _key_id() -> int:
    with connect() as conn:
        return one(conn, "SELECT id FROM api_key ORDER BY id DESC LIMIT 1")["id"]


@jobs.handler("test", "ok")
def _ok(job):
    return {"fine": True}


@jobs.handler("test", "bad")
def _bad(job):
    raise jobs.PermanentError("nope")


def _run_all():
    while jobs.run_once():
        pass


def test_finished_job_sends_signed_event(client, key_for, receiver):
    headers = key_for("render")
    created = client.post("/v1/webhooks", headers=headers, json={"url": receiver.url}).json()
    secret = created["secret"]
    assert secret.startswith("whsec_")
    assert "secret" not in client.get("/v1/webhooks", headers=headers).json()[0]

    job_id = jobs.enqueue(_key_id(), "test", "ok", {})
    _run_all()

    assert len(receiver.requests) == 1
    req = receiver.requests[0]
    event = json.loads(req["body"])
    assert event["type"] == "job.succeeded" and event["data"]["job_id"] == job_id
    ts, v1 = (part.split("=", 1)[1] for part in req["headers"]["Docforge-Signature"].split(","))
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + req["body"], hashlib.sha256).hexdigest()
    assert hmac.compare_digest(v1, expected)
    assert req["headers"]["Docforge-Event-Id"] == event["id"]


def test_failed_job_sends_failed_event(client, key_for, receiver):
    client.post("/v1/webhooks", headers=key_for("render"), json={"url": receiver.url})
    jobs.enqueue(_key_id(), "test", "bad", {})
    _run_all()
    event = json.loads(receiver.requests[0]["body"])
    assert event["type"] == "job.failed" and event["data"]["error"] == "nope"


def test_receiver_5xx_is_retried_and_4xx_is_final(client, key_for, receiver):
    client.post("/v1/webhooks", headers=key_for("render"), json={"url": receiver.url})
    receiver.status = 503
    jobs.enqueue(_key_id(), "test", "ok", {})
    _run_all()
    with connect() as conn:
        delivery = one(conn, "SELECT id, status, attempts FROM job WHERE product = 'webhooks' ORDER BY id DESC LIMIT 1")
    assert delivery["status"] == "queued" and delivery["attempts"] == 1

    receiver.status = 400
    with connect() as conn:
        conn.execute("UPDATE job SET run_after = now() WHERE id = %s", (delivery["id"],))
    _run_all()
    with connect() as conn:
        final = one(conn, "SELECT status, error FROM job WHERE id = %s", (delivery["id"],))
    assert final["status"] == "failed" and "400" in final["error"]


def test_redirects_are_not_followed(client, key_for, receiver):
    client.post("/v1/webhooks", headers=key_for("render"), json={"url": receiver.url})
    receiver.status = 302
    jobs.enqueue(_key_id(), "test", "ok", {})
    _run_all()
    assert len(receiver.requests) == 1  # only our receiver was contacted
    with connect() as conn:
        final = one(conn, "SELECT status FROM job WHERE product = 'webhooks' ORDER BY id DESC LIMIT 1")
    assert final["status"] == "failed"


def test_webhook_jobs_do_not_trigger_more_webhooks(client, key_for, receiver):
    client.post("/v1/webhooks", headers=key_for("render"), json={"url": receiver.url})
    jobs.enqueue(_key_id(), "test", "ok", {})
    _run_all()
    assert len(receiver.requests) == 1


@pytest.mark.parametrize("url", ["http://example.com/hook", "https://user:pw@example.com/x", "ftp://example.com",
                                 "https://127.0.0.1/hook", "https://169.254.169.254/latest"])
def test_unsafe_urls_rejected(client, key_for, monkeypatch, url):
    monkeypatch.setenv("DELIVERY_ALLOW_PRIVATE", "0")
    assert client.post("/v1/webhooks", headers=key_for("render"), json={"url": url}).status_code == 422


def test_endpoints_are_isolated_and_deletable(client, key_for, receiver):
    from docforge.auth import create_key

    headers = key_for("render")
    eid = client.post("/v1/webhooks", headers=headers, json={"url": receiver.url}).json()["id"]
    with connect() as conn:
        other_org = one(conn, "INSERT INTO organization (name) VALUES ('hooky') RETURNING id")["id"]
    other = {"Authorization": f"Bearer {create_key(other_org, ['render'])}"}
    assert client.get("/v1/webhooks", headers=other).json() == []
    assert client.delete(f"/v1/webhooks/{eid}", headers=other).status_code == 404
    assert client.delete(f"/v1/webhooks/{eid}", headers=headers).status_code == 204


def test_sign_matches_documented_format():
    sig = webhooks.sign("whsec_x", 1700000000, b'{"a":1}')
    assert sig == "t=1700000000,v1=" + hmac.new(b"whsec_x", b'1700000000.{"a":1}', hashlib.sha256).hexdigest()


def test_webhooks_get_a_longer_retry_budget(client, key_for, receiver):
    client.post("/v1/webhooks", headers=key_for("render"), json={"url": receiver.url})
    receiver.status = 503
    jobs.enqueue(_key_id(), "test", "ok", {})
    _run_all()
    with connect() as conn:
        delivery_id = one(conn, "SELECT id FROM job WHERE product = 'webhooks' ORDER BY id DESC LIMIT 1")["id"]
    for _ in range(10):
        with connect() as conn:
            conn.execute("UPDATE job SET run_after = now() WHERE id = %s", (delivery_id,))
        _run_all()
    with connect() as conn:
        final = one(conn, "SELECT status, attempts, run_after FROM job WHERE id = %s", (delivery_id,))
    assert final["status"] == "failed" and final["attempts"] == 8
    assert len(receiver.requests) == 8


@jobs.handler("test", "patient", max_attempts=20)
def _patient(job):
    raise ConnectionError("still down")


def test_retry_backoff_is_capped_at_an_hour(key_for):
    from docforge.core.jobs import fail

    key_for("render")
    job_id = jobs.enqueue(_key_id(), "test", "patient", {})
    with connect() as conn:
        conn.execute("UPDATE job SET attempts = 10 WHERE id = %s", (job_id,))  # uncapped: 30s * 2^9 = 4h16m
    fail({"id": job_id, "attempts": 10, "product": "test", "operation": "patient"}, "x", permanent=False)
    with connect() as conn:
        row = one(conn, "SELECT status, extract(epoch FROM run_after - now())::int AS s FROM job WHERE id = %s", (job_id,))
        conn.execute("DELETE FROM job WHERE id = %s", (job_id,))
    assert row["status"] == "queued" and 3500 <= row["s"] <= 3600
