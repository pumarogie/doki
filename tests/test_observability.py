from doki import observability
from doki.core import jobs


def _metrics(client, monkeypatch, token="scrape-me"):
    monkeypatch.setenv("METRICS_TOKEN", token)
    return client.get("/metrics", headers={"Authorization": f"Bearer {token}"})


def test_metrics_disabled_without_token(client, monkeypatch):
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    assert client.get("/metrics").status_code == 404


def test_metrics_require_the_token(client, monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "right")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_requests_counted_by_route_template(client, key_for, monkeypatch):
    headers = key_for("render")
    client.get("/v1/templates/123456", headers=headers)
    body = _metrics(client, monkeypatch).text
    assert 'route="/v1/templates/{template_id}"' in body
    assert "/v1/templates/123456" not in body
    assert "doki_jobs_queued" in body


def test_job_outcomes_recorded(client, key_for):
    from doki.db import connect, one

    key_for("extract")
    with connect() as conn:
        key_id = one(conn, "SELECT id FROM api_key ORDER BY id DESC LIMIT 1")["id"]

    @jobs.handler("test", "boom")
    def _boom(job):
        raise jobs.PermanentError("nope")

    before = observability.JOBS.labels("test", "boom", "failed")._value.get()
    jobs.enqueue(key_id, "test", "boom", {})
    while jobs.run_once():
        pass
    assert observability.JOBS.labels("test", "boom", "failed")._value.get() == before + 1


def test_sentry_off_without_dsn(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert observability.init_sentry("test") is False
