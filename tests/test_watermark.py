import shutil
import subprocess

import pytest

from docforge.core import jobs, watermark
from docforge.db import connect, one

TYPST = "= Watermarked"


def test_rendered_pdf_carries_the_request_id(client, key_for):
    r = client.post("/v1/render", headers={**key_for("render"), "X-Request-ID": "trace-render-1"},
                    json={"engine": "typst", "source": TYPST})
    assert r.status_code == 200
    assert watermark.read(r.content) == "trace-render-1"


@pytest.mark.skipif(not shutil.which("verapdf"), reason="needs verapdf")
def test_watermark_keeps_pdfa_valid(client, key_for):
    headers = key_for("render", "pdfa")
    pdf = client.post("/v1/render", headers={**headers, "X-Request-ID": "trace-pdfa"},
                      json={"engine": "typst", "source": TYPST, "pdfa": True}).content
    assert watermark.read(pdf) == "trace-pdfa"
    report = client.post("/v1/pdfa/validate", headers=headers, files={"file": ("w.pdf", pdf, "application/pdf")}).json()
    assert report["compliant"] is True, report


@pytest.mark.skipif(not shutil.which("gs"), reason="needs ghostscript")
def test_converted_pdf_is_stamped_and_traceable(client, key_for, tmp_path):
    from docforge.auth import trace

    src = tmp_path / "p.pdf"
    subprocess.run(["gs", "-q", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite", f"-sOutputFile={src}",
                    "-c", "/Helvetica findfont 12 scalefont setfont 72 700 moveto (x) show showpage"], check=True)
    headers = {**key_for("pdfa"), "X-Request-ID": "trace-convert-9"}
    out = client.post("/v1/pdfa/convert", headers=headers, files={"file": ("p.pdf", src.read_bytes(), "application/pdf")})
    assert watermark.read(out.content) == "trace-convert-9"
    found = trace("trace-convert-9")
    assert found is not None
    assert found["prefix"] == headers["Authorization"].split("_")[1]
    assert found["product"] == "pdfa" and found["operation"] == "convert"


def test_scheduled_report_is_stamped_with_job_id(client, key_for, monkeypatch):
    headers = {**key_for("reports", "render")}
    tid = client.post("/v1/templates", headers=headers, json={"name": "wm", "engine": "typst", "source": TYPST}).json()["id"]
    sid = client.post("/v1/schedules", headers=headers, json={
        "name": "wm", "template_id": tid, "cron": "0 7 * * *",
        "delivery": {"type": "email", "to": ["x@example.com"]},
    }).json()["id"]
    job_id = client.post(f"/v1/schedules/{sid}/run", headers=headers).json()["job_id"]

    from docforge.core import deliver

    monkeypatch.setattr(deliver, "email", lambda *a, **k: None)
    while jobs.run_once():
        pass
    with connect() as conn:
        file_id = one(conn, "SELECT output_file_id FROM job WHERE id = %s", (job_id,))["output_file_id"]
    pdf = client.get(f"/v1/files/{file_id}", headers=headers).content
    assert watermark.read(pdf) == f"job-{job_id}"


def test_no_trace_leaves_pdf_untouched():
    from docforge.core import render

    pdf = render.render("typst", TYPST, {})
    assert watermark.stamp(pdf, None) == pdf
    assert watermark.read(pdf) is None
