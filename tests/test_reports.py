import shutil
import socket
import subprocess
import time
import uuid

import pytest
from aiosmtpd.controller import Controller

from doki.core import deliver, jobs
from doki.db import connect, one
from doki.products import reports

TYPST = '#let d = json(bytes(sys.inputs.data))\n= Report #d.period'


class Inbox:
    def __init__(self):
        self.messages = []

    async def handle_DATA(self, server, session, envelope):
        self.messages.append(envelope)
        return "250 OK"


@pytest.fixture(scope="module")
def smtp(monkeypatch_module):
    inbox = Inbox()
    port = _free_port()
    controller = Controller(inbox, hostname="127.0.0.1", port=port)
    controller.start()
    monkeypatch_module.setenv("SMTP_HOST", "127.0.0.1")
    monkeypatch_module.setenv("SMTP_PORT", str(port))
    yield inbox
    controller.stop()


@pytest.fixture(scope="module")
def monkeypatch_module():
    with pytest.MonkeyPatch.context() as mp:
        yield mp


@pytest.fixture(autouse=True)
def empty_queue(client):
    while jobs.run_once():
        pass


@pytest.fixture
def template_id(client, key_for):
    headers = key_for("render")
    return client.post("/v1/templates", headers=headers,
                       json={"name": f"report-{uuid.uuid4().hex[:6]}", "engine": "typst", "source": TYPST}).json()["id"]


def _schedule(tid, **overrides):
    body = {"name": "monthly", "template_id": tid, "cron": "0 7 1 * *", "timezone": "Europe/Berlin",
            "data": {"period": "2026-09"}, "delivery": {"type": "email", "to": ["cfo@example.com"]}}
    return {**body, **overrides}


def _due_now(schedule_id):
    with connect() as conn:
        conn.execute("UPDATE schedule SET next_run_at = now() - interval '1 second' WHERE id = %s", (schedule_id,))


def test_email_schedule_runs_when_due(client, org_id, key_for, template_id, smtp):
    headers = key_for("reports")
    s = client.post("/v1/schedules", headers=headers, json=_schedule(template_id))
    assert s.status_code == 201, s.text
    sid = s.json()["id"]

    assert reports.enqueue_due() == 0  # not due yet
    _due_now(sid)
    assert reports.enqueue_due() == 1
    assert reports.enqueue_due() == 0  # next_run_at moved forward

    before = len(smtp.messages)
    assert jobs.run_once()
    assert len(smtp.messages) == before + 1
    msg = smtp.messages[-1]
    assert msg.rcpt_tos == ["cfo@example.com"] and b"application/pdf" in msg.content

    with connect() as conn:
        job = one(conn, "SELECT status, result, output_file_id FROM job WHERE product = 'reports' ORDER BY id DESC LIMIT 1")
    assert job["status"] == "done" and job["result"]["delivered_to"] == "cfo@example.com"
    pdf = client.get(f"/v1/files/{job['output_file_id']}", headers=headers)
    assert pdf.content.startswith(b"%PDF-")


def test_next_run_respects_timezone():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    after = datetime(2026, 9, 16, 12, 0, tzinfo=ZoneInfo("UTC"))
    nxt = reports.next_run("0 7 * * *", "Europe/Berlin", after)
    assert nxt.astimezone(ZoneInfo("UTC")) == datetime(2026, 9, 17, 5, 0, tzinfo=ZoneInfo("UTC"))


def test_sftp_password_is_encrypted_and_never_returned(client, key_for, template_id):
    headers = key_for("reports")
    delivery = {"type": "sftp", "host": "sftp.example.com", "username": "u", "password": "hunter2-secret",
                "host_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"}
    r = client.post("/v1/schedules", headers=headers, json=_schedule(template_id, delivery=delivery))
    assert r.status_code == 201, r.text
    assert "hunter2" not in r.text and "password" not in r.json()["delivery"]
    assert "hunter2" not in client.get("/v1/schedules", headers=headers).text
    with connect() as conn:
        stored = one(conn, "SELECT delivery::text AS d FROM schedule WHERE id = %s", (r.json()["id"],))["d"]
    assert "hunter2" not in stored and "password_enc" in stored


@pytest.mark.parametrize("patch", [
    {"cron": "every day"},
    {"cron": "* * * * * *"},
    {"timezone": "Mars/Olympus"},
    {"delivery": {"type": "email", "to": []}},
    {"delivery": {"type": "email", "to": ["not-an-email"]}},
    {"delivery": {"type": "fax", "to": "123"}},
    {"template_id": 999999},
])
def test_invalid_schedules_rejected(client, key_for, template_id, patch):
    assert client.post("/v1/schedules", headers=key_for("reports"), json=_schedule(template_id, **patch)).status_code == 422


def test_revoked_key_stops_schedule(client, key_for, template_id):
    headers = key_for("reports")
    sid = client.post("/v1/schedules", headers=headers, json=_schedule(template_id)).json()["id"]
    _due_now(sid)
    prefix = headers["Authorization"].split("_")[1]
    with connect() as conn:
        conn.execute("UPDATE api_key SET revoked_at = now() WHERE prefix = %s", (prefix,))
    assert reports.enqueue_due() == 0


def test_template_in_use_cannot_be_deleted(client, key_for, template_id):
    client.post("/v1/schedules", headers=key_for("reports"), json=_schedule(template_id))
    assert client.delete(f"/v1/templates/{template_id}", headers=key_for("render")).status_code == 409


def test_schedule_crud_and_isolation(client, key_for, template_id):
    from doki.auth import create_key

    headers = key_for("reports")
    sid = client.post("/v1/schedules", headers=headers, json=_schedule(template_id)).json()["id"]
    updated = client.put(f"/v1/schedules/{sid}", headers=headers, json=_schedule(template_id, name="weekly", cron="0 8 * * 1"))
    assert updated.json()["name"] == "weekly"
    with connect() as conn:
        other_org = one(conn, "INSERT INTO organization (name) VALUES ('snoop') RETURNING id")["id"]
    other = {"Authorization": f"Bearer {create_key(other_org, ['reports'])}"}
    for method, path in [("get", f"/v1/schedules/{sid}"), ("delete", f"/v1/schedules/{sid}"), ("post", f"/v1/schedules/{sid}/run")]:
        assert getattr(client, method)(path, headers=other).status_code == 404
    assert client.delete(f"/v1/schedules/{sid}", headers=headers).status_code == 204


def test_private_sftp_hosts_blocked(monkeypatch):
    monkeypatch.setenv("DELIVERY_ALLOW_PRIVATE", "0")
    for host in ["127.0.0.1", "localhost", "10.1.2.3", "169.254.169.254"]:
        with pytest.raises(deliver.DeliveryError, match="public"):
            deliver.check_public_host(host)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def sftp_server():
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True, check=False).returncode != 0:
        pytest.skip("needs docker")
    port = _free_port()
    name = f"doki-sftp-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:22",
                    "atmoz/sftp:alpine", "user:pass:::upload"], check=True, capture_output=True)
    try:
        host_key = ""
        for _ in range(60):
            scan = subprocess.run(["ssh-keyscan", "-t", "ed25519", "-p", str(port), "127.0.0.1"], capture_output=True, text=True, check=False)
            line = next((ln for ln in scan.stdout.splitlines() if not ln.startswith("#") and "ssh-ed25519" in ln), "")
            if line:
                host_key = " ".join(line.split()[1:3])
                break
            time.sleep(1)
        if not host_key:
            pytest.skip("sftp container did not start")
        yield {"port": port, "host_key": host_key, "name": name}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def test_sftp_delivery_end_to_end(client, key_for, template_id, sftp_server):
    headers = key_for("reports")
    delivery = {"type": "sftp", "host": "127.0.0.1", "port": sftp_server["port"], "username": "user",
                "password": "pass", "host_key": sftp_server["host_key"], "directory": "upload"}
    sid = client.post("/v1/schedules", headers=headers, json=_schedule(template_id, name="sftp-report", delivery=delivery)).json()["id"]
    job_id = client.post(f"/v1/schedules/{sid}/run", headers=headers).json()["job_id"]
    assert jobs.run_once()
    job = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
    assert job["status"] == "done", job
    listing = subprocess.run(["docker", "exec", sftp_server["name"], "ls", "/home/user/upload"], capture_output=True, text=True, check=True).stdout
    assert "sftp-report-" in listing and listing.strip().endswith(".pdf")


def test_sftp_wrong_host_key_fails_permanently(client, key_for, template_id, sftp_server):
    headers = key_for("reports")
    wrong = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
    delivery = {"type": "sftp", "host": "127.0.0.1", "port": sftp_server["port"], "username": "user",
                "password": "pass", "host_key": wrong, "directory": "upload"}
    sid = client.post("/v1/schedules", headers=headers, json=_schedule(template_id, delivery=delivery)).json()["id"]
    job_id = client.post(f"/v1/schedules/{sid}/run", headers=headers).json()["job_id"]
    jobs.run_once()
    job = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
    assert job["status"] == "failed" and job["error"] == "SFTP host key does not match"
