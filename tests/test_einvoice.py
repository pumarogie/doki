import re
import shutil
import subprocess
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


def _validate(client, headers, name, data, mime):
    return client.post("/v1/einvoice/validate", headers=headers, files={"file": (name, data, mime)})


@pytest.mark.parametrize("fixture", ["valid_cii.xml", "valid_ubl.xml"])
def test_official_samples_are_valid(client, key_for, fixture):
    r = _validate(client, key_for("einvoice"), fixture, (FIXTURES / fixture).read_bytes(), "application/xml")
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True and body["errors"] == [], body
    assert body["source"] == "xml"


def test_missing_buyer_reference_breaks_xrechnung_rule(client, key_for, cii):
    broken = re.sub(rb"<ram:BuyerReference>.*?</ram:BuyerReference>", b"", cii)
    body = _validate(client, key_for("einvoice"), "broken.xml", broken, "application/xml").json()
    assert body["valid"] is False
    assert "BR-DE-15" in {e["rule"] for e in body["errors"]}


def test_zugferd_roundtrip(client, key_for, cii, invoice_pdf):
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
