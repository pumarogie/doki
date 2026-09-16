import uuid

from docforge.core import retention
from docforge.db import connect, one

BODY = {"engine": "typst", "source": "= Idempotent"}


def _usage_calls(client, headers) -> int:
    return sum(u["calls"] for u in client.get("/v1/usage", headers=headers).json()["usage"])


def test_retry_replays_response_without_billing_twice(client, key_for):
    headers = {**key_for("render"), "Idempotency-Key": f"k-{uuid.uuid4()}"}
    first = client.post("/v1/render", headers=headers, json=BODY)
    assert first.status_code == 200 and "idempotent-replayed" not in first.headers
    calls = _usage_calls(client, headers)

    second = client.post("/v1/render", headers=headers, json=BODY)
    assert second.status_code == 200
    assert second.headers["idempotent-replayed"] == "true"
    assert second.headers["content-type"] == "application/pdf"
    assert second.content == first.content
    assert _usage_calls(client, headers) == calls


def test_same_key_different_request_is_rejected(client, key_for):
    headers = {**key_for("render"), "Idempotency-Key": "reuse-me"}
    assert client.post("/v1/render", headers=headers, json=BODY).status_code == 200
    r = client.post("/v1/render", headers=headers, json={**BODY, "source": "= Other"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "idempotency_key_reused"


def test_request_in_progress_gets_409_until_stale(client, key_for):
    headers = {**key_for("render"), "Idempotency-Key": "in-flight"}
    assert client.post("/v1/render", headers=headers, json=BODY).status_code == 200
    with connect() as conn:  # pretend the first request is still running
        conn.execute("UPDATE idempotency_key SET completed_at = NULL, status_code = NULL WHERE key = 'in-flight'")
    r = client.post("/v1/render", headers=headers, json=BODY)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "request_in_progress"

    with connect() as conn:  # the original worker died long ago: the key can be taken over
        conn.execute("UPDATE idempotency_key SET created_at = now() - interval '1 hour' WHERE key = 'in-flight'")
    r = client.post("/v1/render", headers=headers, json=BODY)
    assert r.status_code == 200 and "idempotent-replayed" not in r.headers


def test_client_errors_are_replayed_but_server_errors_are_not(client, key_for):
    headers = {**key_for("render"), "Idempotency-Key": "bad-template"}
    bad = {"engine": "typst", "source": "#let x = "}
    assert client.post("/v1/render", headers=headers, json=bad).status_code == 422
    replay = client.post("/v1/render", headers=headers, json=bad)
    assert replay.status_code == 422 and replay.headers["idempotent-replayed"] == "true"


def test_keys_are_scoped_per_api_key(client, key_for):
    a = {**key_for("render"), "Idempotency-Key": "shared"}
    b = {**key_for("render"), "Idempotency-Key": "shared"}
    assert client.post("/v1/render", headers=a, json=BODY).status_code == 200
    r = client.post("/v1/render", headers=b, json={**BODY, "source": "= B"})
    assert r.status_code == 200 and "idempotent-replayed" not in r.headers


def test_extract_job_submit_is_not_duplicated(client, key_for):
    import subprocess
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp, "i.pdf")
        subprocess.run(["gs", "-q", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite", f"-sOutputFile={out}",
                        "-c", "/Helvetica findfont 12 scalefont setfont 72 700 moveto (x) show showpage"], check=True)
        pdf = out.read_bytes()
    headers = {**key_for("extract"), "Idempotency-Key": "submit-once"}
    files = {"file": ("i.pdf", pdf, "application/pdf")}
    first = client.post("/v1/extract", headers=headers, files=files).json()["job_id"]
    second = client.post("/v1/extract", headers=headers, files=files)
    assert second.json()["job_id"] == first and second.headers["idempotent-replayed"] == "true"
    with connect() as conn:
        assert one(conn, "SELECT count(*) AS n FROM job WHERE id >= %s", (first,))["n"] == 1


def test_get_requests_and_missing_auth_are_untouched(client):
    assert "idempotent-replayed" not in client.get("/health", headers={"Idempotency-Key": "x"}).headers
    r = client.post("/v1/render", headers={"Idempotency-Key": "x"}, json=BODY)
    assert r.status_code == 401


def test_overlong_key_rejected(client, key_for):
    r = client.post("/v1/render", headers={**key_for("render"), "Idempotency-Key": "k" * 256}, json=BODY)
    assert r.status_code == 422


def test_expired_keys_are_purged(client, key_for):
    headers = {**key_for("render"), "Idempotency-Key": "old"}
    client.post("/v1/render", headers=headers, json=BODY)
    with connect() as conn:
        conn.execute("UPDATE idempotency_key SET created_at = now() - interval '2 days' WHERE key = 'old'")
    retention.purge()
    with connect() as conn:
        assert conn.execute("SELECT 1 FROM idempotency_key WHERE key = 'old'").fetchone() is None


def test_fingerprint_ignores_multipart_boundary():
    from docforge.idempotency import fingerprint

    scope = {"method": "POST", "path": "/v1/extract", "query_string": b""}
    a = fingerprint(scope, b"multipart/form-data; boundary=aaa", b"--aaa\r\nx\r\n--aaa--")
    b = fingerprint(scope, b"multipart/form-data; boundary=bbb", b"--bbb\r\nx\r\n--bbb--")
    c = fingerprint(scope, b"multipart/form-data; boundary=bbb", b"--bbb\r\ny\r\n--bbb--")
    assert a == b != c
