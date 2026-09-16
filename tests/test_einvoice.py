import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from docforge.core.einvoice import KOSIT_DIR

FIXTURES = Path(__file__).parent / "fixtures" / "xrechnung"

pytestmark = pytest.mark.skipif(
    not ((KOSIT_DIR / "validator.jar").is_file() and shutil.which("java") and shutil.which("gs") and shutil.which("verapdf")),
    reason="needs KoSIT (scripts/fetch-kosit.sh), java, ghostscript and verapdf",
)


@pytest.fixture(scope="module")
def cii() -> bytes:
    return (FIXTURES / "valid_cii.xml").read_bytes()


@pytest.fixture(scope="module")
def invoice_pdf(tmp_path_factory) -> bytes:
    out = tmp_path_factory.mktemp("pdf") / "invoice.pdf"
    subprocess.run(
        ["gs", "-q", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite", f"-sOutputFile={out}",
         "-c", "/Helvetica findfont 20 scalefont setfont 72 700 moveto (Invoice 1) show showpage"],
        check=True,
    )
    return out.read_bytes()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def kosit_daemon():
    port = _free_port()
    proc = subprocess.Popen(
        ["java", "-jar", str(KOSIT_DIR / "validator.jar"), "-s", str(KOSIT_DIR / "config" / "scenarios.xml"),
         "-r", str(KOSIT_DIR / "config"), "-D", "-G", "-H", "127.0.0.1", "-P", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(url, timeout=1)
                break
            except urllib.error.HTTPError:
                break  # any HTTP answer means the daemon is up
            except (urllib.error.URLError, OSError):
                time.sleep(0.5)
        else:
            pytest.fail("KoSIT daemon did not start")
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(params=["cli", "daemon"])
def kosit_mode(request, monkeypatch):
    if request.param == "daemon":
        monkeypatch.setenv("KOSIT_URL", request.getfixturevalue("kosit_daemon"))
    else:
        monkeypatch.delenv("KOSIT_URL", raising=False)
    return request.param


def _validate(client, headers, name, data, mime):
    return client.post("/v1/einvoice/validate", headers=headers, files={"file": (name, data, mime)})


@pytest.mark.parametrize("fixture", ["valid_cii.xml", "valid_ubl.xml"])
def test_official_samples_are_valid(client, key_for, fixture, kosit_mode):
    r = _validate(client, key_for("einvoice"), fixture, (FIXTURES / fixture).read_bytes(), "application/xml")
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True and body["errors"] == [], body
    assert body["source"] == "xml"


def test_missing_buyer_reference_breaks_xrechnung_rule(client, key_for, cii, kosit_mode):
    broken = re.sub(rb"<ram:BuyerReference>.*?</ram:BuyerReference>", b"", cii)
    body = _validate(client, key_for("einvoice"), "broken.xml", broken, "application/xml").json()
    assert body["valid"] is False
    assert "BR-DE-15" in {e["rule"] for e in body["errors"]}


def test_zugferd_roundtrip(client, key_for, cii, invoice_pdf, kosit_mode):
    headers = key_for("einvoice", "pdfa")
    r = client.post("/v1/einvoice/zugferd", headers=headers, files={
        "pdf": ("invoice.pdf", invoice_pdf, "application/pdf"),
        "xml": ("invoice.xml", cii, "application/xml"),
    })
    assert r.status_code == 200, r.text
    hybrid = r.content

    pdfa_report = client.post("/v1/pdfa/validate", headers=headers, files={"file": ("h.pdf", hybrid, "application/pdf")}).json()
    assert pdfa_report["compliant"] is True and "3" in pdfa_report["flavour"]

    body = _validate(client, headers, "h.pdf", hybrid, "application/pdf").json()
    assert body["valid"] is True and body["source"] == "pdf"


def test_pdf_without_invoice_xml(client, key_for, invoice_pdf):
    r = _validate(client, key_for("einvoice"), "plain.pdf", invoice_pdf, "application/pdf")
    assert r.status_code == 422


def test_zugferd_rejects_non_cii_xml(client, key_for, invoice_pdf):
    r = client.post("/v1/einvoice/zugferd", headers=key_for("einvoice"), files={
        "pdf": ("invoice.pdf", invoice_pdf, "application/pdf"),
        "xml": ("x.xml", b"<foo/>", "application/xml"),
    })
    assert r.status_code == 422


def test_rejects_non_invoice_upload(client, key_for):
    assert _validate(client, key_for("einvoice"), "a.txt", b"hello", "text/plain").status_code == 415


def test_requires_einvoice_scope(client, key_for, cii):
    assert _validate(client, key_for("pdfa"), "a.xml", cii, "application/xml").status_code == 403


def test_daemon_is_fast(client, key_for, cii, kosit_daemon, monkeypatch):
    monkeypatch.setenv("KOSIT_URL", kosit_daemon)
    headers = key_for("einvoice")
    _validate(client, headers, "warm.xml", cii, "application/xml")
    started = time.monotonic()
    for _ in range(3):
        assert _validate(client, headers, "a.xml", cii, "application/xml").json()["valid"] is True
    assert (time.monotonic() - started) / 3 < 1.5


def test_daemon_down_gives_generic_422(client, key_for, cii, monkeypatch):
    monkeypatch.setenv("KOSIT_URL", "http://127.0.0.1:9")  # nothing listens on the discard port
    r = _validate(client, key_for("einvoice"), "a.xml", cii, "application/xml")
    assert r.status_code == 422 and "127.0.0.1" not in r.text
