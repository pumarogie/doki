import json
import shutil
import subprocess

import pytest

from doki.core import jobs, llm
from doki.core.extract import SCHEMAS
from doki.db import connect, one

pytestmark = pytest.mark.skipif(not shutil.which("gs"), reason="needs ghostscript to build fixture PDFs")

LINES = ["Invoice number: RE-2026-042", "Invoice date: 2026-09-01", "Gross total: 119.00"]


@pytest.fixture(scope="module")
def invoice_pdf(tmp_path_factory) -> bytes:
    out = tmp_path_factory.mktemp("pdf") / "invoice.pdf"
    ps = " ".join(f"72 {700 - 30 * i} moveto ({line}) show" for i, line in enumerate(LINES))
    subprocess.run(
        ["gs", "-q", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite", f"-sOutputFile={out}",
         "-c", f"/Helvetica findfont 14 scalefont setfont {ps} showpage"],
        check=True,
    )
    return out.read_bytes()


@pytest.fixture(autouse=True)
def empty_queue(client):
    while jobs.run_once():
        pass


def _submit(client, headers, pdf, **form):
    return client.post("/v1/extract", headers=headers, files={"file": ("inv.pdf", pdf, "application/pdf")}, data=form)


def test_extract_job_lifecycle(client, key_for, invoice_pdf):
    headers = key_for("extract")
    r = _submit(client, headers, invoice_pdf, schema="invoice", verify="true")
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert client.get(f"/v1/jobs/{job_id}", headers=headers).json()["status"] == "queued"

    assert jobs.run_once() is True
    job = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
    assert job["status"] == "done", job
    fields = {f["name"]: f for f in job["result"]["fields"]}
    assert fields["invoice_number"]["value"] == "RE-2026-042"
    assert fields["invoice_number"]["source_page"] == 1
    assert fields["invoice_number"]["confidence"] >= 0.9
    assert fields["iban"]["value"] is None
    assert "iban" in job["result"]["uncertain_fields"]
    assert "invoice_number" not in job["result"]["uncertain_fields"]
    assert job["result"]["verified"] is True
    assert list(fields) == list(SCHEMAS["invoice"])

    usage = client.get("/v1/usage", headers=headers).json()["usage"]
    assert any(u["product"] == "extract" and u["pages"] >= 1 for u in usage)


def test_custom_schema(client, key_for, invoice_pdf):
    headers = key_for("extract")
    job_id = _submit(client, headers, invoice_pdf, schema="custom",
                     fields=json.dumps({"gross_total": "amount due"})).json()["job_id"]
    jobs.run_once()
    result = client.get(f"/v1/jobs/{job_id}", headers=headers).json()["result"]
    assert result["fields"] == [{"name": "gross_total", "value": "119.00", "confidence": 0.95,
                                 "source_page": 1, "reason": None}]


@pytest.mark.parametrize("form", [
    {"schema": "passport"},
    {"schema": "custom"},
    {"schema": "custom", "fields": "not json"},
    {"schema": "custom", "fields": json.dumps({"bad key": "x"})},
    {"schema": "custom", "fields": json.dumps({f"f{i}": "x" for i in range(51)})},
])
def test_invalid_schema_requests(client, key_for, invoice_pdf, form):
    assert _submit(client, key_for("extract"), invoice_pdf, **form).status_code == 422


def test_unverified_value_loses_confidence(client, key_for, invoice_pdf, monkeypatch):
    monkeypatch.setattr(llm, "verify_fields", lambda pdf, fields: {f["name"]: False for f in fields})
    headers = key_for("extract")
    job_id = _submit(client, headers, invoice_pdf, schema="invoice", verify="true").json()["job_id"]
    jobs.run_once()
    fields = {f["name"]: f for f in client.get(f"/v1/jobs/{job_id}", headers=headers).json()["result"]["fields"]}
    assert fields["invoice_number"]["confidence"] <= 0.3
    assert "verification" in fields["invoice_number"]["reason"]


def test_transient_failure_retries_then_fails(client, key_for, invoice_pdf, monkeypatch):
    def boom(pdf, fields):
        raise ConnectionError("upstream down: secret-host.internal")

    monkeypatch.setattr(llm, "extract_fields", boom)
    headers = key_for("extract")
    job_id = _submit(client, headers, invoice_pdf).json()["job_id"]

    job: dict = {}
    for attempt in range(1, jobs.MAX_ATTEMPTS + 1):
        with connect() as conn:  # skip the backoff wait
            conn.execute("UPDATE job SET run_after = now() WHERE id = %s", (job_id,))
        assert jobs.run_once()
        job = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
        assert job["attempts"] == attempt
        assert job["status"] == ("failed" if attempt == jobs.MAX_ATTEMPTS else "queued")
    assert job["error"] == "processing failed"
    assert "secret-host" not in json.dumps(job)


def test_model_refusal_fails_permanently(client, key_for, invoice_pdf, monkeypatch):
    def refuse(pdf, fields):
        raise llm.LLMError("model declined this document")

    monkeypatch.setattr(llm, "extract_fields", refuse)
    headers = key_for("extract")
    job_id = _submit(client, headers, invoice_pdf).json()["job_id"]
    jobs.run_once()
    job = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
    assert job["status"] == "failed" and job["attempts"] == 1


def test_stale_running_job_is_requeued(client, key_for, invoice_pdf):
    headers = key_for("extract")
    job_id = _submit(client, headers, invoice_pdf).json()["job_id"]
    with connect() as conn:
        conn.execute("UPDATE job SET status = 'running', started_at = now() - interval '1 hour' WHERE id = %s", (job_id,))
    assert jobs.run_once()
    assert client.get(f"/v1/jobs/{job_id}", headers=headers).json()["status"] == "done"


def test_jobs_and_files_are_isolated_per_organization(client, key_for, invoice_pdf):
    from doki.auth import create_key

    headers = key_for("extract")
    job_id = _submit(client, headers, invoice_pdf).json()["job_id"]
    jobs.run_once()
    with connect() as conn:
        file_id = one(conn, "SELECT input_file_id FROM job WHERE id = %s", (job_id,))["input_file_id"]
        other_org = one(conn, "INSERT INTO organization (name) VALUES ('intruder') RETURNING id")["id"]
    assert client.get(f"/v1/files/{file_id}", headers=headers).content == invoice_pdf
    other = {"Authorization": f"Bearer {create_key(other_org, ['extract'])}"}
    assert client.get(f"/v1/jobs/{job_id}", headers=other).status_code == 404
    assert client.get(f"/v1/files/{file_id}", headers=other).status_code == 404


def test_requires_extract_scope(client, key_for, invoice_pdf):
    assert _submit(client, key_for("render"), invoice_pdf).status_code == 403


def test_submit_over_quota_is_rejected_before_work(client, key_for, invoice_pdf):
    headers = key_for("extract", monthly_pages=1)
    first = _submit(client, headers, invoice_pdf)
    assert first.status_code == 202
    jobs.run_once()  # charges 1 page
    assert _submit(client, headers, invoice_pdf).status_code == 402
    with connect() as conn:
        queued = one(conn, "SELECT count(*) AS n FROM job WHERE status = 'queued'")["n"]
    assert queued == 0
