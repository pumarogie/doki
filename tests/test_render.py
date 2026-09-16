import io
import shutil

import pytest
from pypdf import PdfReader

TYPST = '#let d = json(bytes(sys.inputs.data))\n= Invoice #d.number\n#for i in d.items [ - #i ]'
HTML = "<h1>Invoice {{ data.number }}</h1>{% for i in data['items'] %}<p>{{ i }}</p>{% endfor %}"
DATA = {"number": "A-17", "items": ["Bolt", "Nut"]}


def _text(pdf: bytes) -> str:
    return " ".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


def test_inline_typst_renders_data(client, key_for):
    r = client.post("/v1/render", headers=key_for("render"), json={"engine": "typst", "source": TYPST, "data": DATA})
    assert r.status_code == 200, r.text
    text = _text(r.content)
    assert "A-17" in text and "Bolt" in text


@pytest.mark.skipif(not shutil.which("verapdf"), reason="needs verapdf")
def test_typst_pdfa_output_is_compliant(client, key_for):
    headers = key_for("render", "pdfa")
    pdf = client.post("/v1/render", headers=headers, json={"engine": "typst", "source": TYPST, "data": DATA, "pdfa": True}).content
    report = client.post("/v1/pdfa/validate", headers=headers, files={"file": ("d.pdf", pdf, "application/pdf")}).json()
    assert report["compliant"] is True, report


def test_inline_html_renders_escaped_data_without_js(client, key_for):
    source = HTML + "<script>document.body.innerHTML='JS RAN'</script>"
    data = {"number": "<b>A-17</b>", "items": ["Bolt"]}
    r = client.post("/v1/render", headers=key_for("render"), json={"engine": "html", "source": source, "data": data})
    assert r.status_code == 200, r.text
    text = _text(r.content)
    assert "<b>A-17</b>" in text and "JS RAN" not in text


def test_broken_template_returns_author_error_without_server_paths(client, key_for):
    r = client.post("/v1/render", headers=key_for("render"), json={"engine": "typst", "source": '#read("/etc/hosts")'})
    assert r.status_code == 422
    detail = r.json()["detail"]["template_error"]
    assert "not found" in detail and "/var/" not in detail and "/tmp" not in detail


def test_slow_template_is_stopped(client, key_for):
    from docforge.core import render as engine

    with pytest.raises(engine.TemplateError, match="longer than"):
        engine.render("typst", "#for i in range(1000000000) { [x] }", {}, timeout=2)


def test_template_crud_and_render_by_name(client, key_for):
    headers = key_for("render")
    created = client.post("/v1/templates", headers=headers, json={"name": "invoice", "engine": "typst", "source": TYPST})
    assert created.status_code == 201
    tid = created.json()["id"]
    assert client.post("/v1/templates", headers=headers, json={"name": "invoice", "engine": "html", "source": HTML}).status_code == 409
    assert [t["name"] for t in client.get("/v1/templates", headers=headers).json()] == ["invoice"]

    r = client.post("/v1/render", headers=headers, json={"template_name": "invoice", "data": DATA})
    assert r.status_code == 200 and "A-17" in _text(r.content)

    updated = client.put(f"/v1/templates/{tid}", headers=headers, json={"name": "invoice", "engine": "html", "source": HTML})
    assert updated.json()["engine"] == "html"
    r = client.post("/v1/render", headers=headers, json={"template_id": tid, "data": DATA})
    assert "A-17" in _text(r.content)

    assert client.delete(f"/v1/templates/{tid}", headers=headers).status_code == 204
    assert client.get(f"/v1/templates/{tid}", headers=headers).status_code == 404


def test_templates_are_isolated_per_organization(client, key_for):
    from docforge.auth import create_key
    from docforge.db import connect, one

    tid = client.post("/v1/templates", headers=key_for("render"), json={"name": "private", "engine": "typst", "source": TYPST}).json()["id"]
    with connect() as conn:
        other_org = one(conn, "INSERT INTO organization (name) VALUES ('other') RETURNING id")["id"]
    other = {"Authorization": f"Bearer {create_key(other_org, ['render'])}"}
    assert client.get(f"/v1/templates/{tid}", headers=other).status_code == 404
    assert client.post("/v1/render", headers=other, json={"template_id": tid}).status_code == 404
    assert client.delete(f"/v1/templates/{tid}", headers=other).status_code == 404


@pytest.mark.parametrize("body", [
    {},
    {"engine": "typst"},
    {"template_id": 1, "engine": "typst", "source": "x"},
    {"engine": "html", "source": "<p/>", "pdfa": True},
])
def test_invalid_render_requests(client, key_for, body):
    assert client.post("/v1/render", headers=key_for("render"), json=body).status_code == 422


def test_requires_render_scope(client, key_for):
    assert client.get("/v1/templates", headers=key_for("pdfa")).status_code == 403


def test_html_renders_reuse_one_browser(client, key_for):
    import time

    from docforge.core.render import chromium

    headers = key_for("render")
    client.post("/v1/render", headers=headers, json={"engine": "html", "source": "<p>warm-up</p>"})
    first = chromium.browser_id()
    started = time.monotonic()
    for i in range(5):
        r = client.post("/v1/render", headers=headers, json={"engine": "html", "source": f"<p>{i}</p>"})
        assert r.status_code == 200
    assert time.monotonic() - started < 3
    assert chromium.browser_id() == first


def test_html_render_survives_browser_crash(client, key_for):
    import subprocess

    from docforge.core.render import chromium

    headers = key_for("render")
    assert client.post("/v1/render", headers=headers, json={"engine": "html", "source": "<p>a</p>"}).status_code == 200
    before = chromium.browser_id()
    killed = subprocess.run(["pkill", "-f", "headless[_-]shell"], check=False).returncode == 0
    assert killed, "no chromium process found to kill"
    r = client.post("/v1/render", headers=headers, json={"engine": "html", "source": "<p>b</p>"})
    assert r.status_code == 200 and r.content.startswith(b"%PDF-")
    assert chromium.browser_id() != before
