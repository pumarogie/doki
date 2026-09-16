import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    not (shutil.which("gs") and shutil.which("verapdf")), reason="needs ghostscript and verapdf"
)


@pytest.fixture(scope="module")
def plain_pdf(tmp_path_factory) -> bytes:
    out = tmp_path_factory.mktemp("pdf") / "plain.pdf"
    subprocess.run(
        ["gs", "-q", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite", f"-sOutputFile={out}",
         "-c", "/Helvetica findfont 24 scalefont setfont 72 700 moveto (Hello) show showpage"],
        check=True,
    )
    return out.read_bytes()


def _post(client, path, headers, pdf, **form):
    return client.post(path, headers=headers, files={"file": ("in.pdf", pdf, "application/pdf")}, data=form)


def test_plain_pdf_is_not_compliant(client, key_for, plain_pdf):
    r = _post(client, "/v1/pdfa/validate", key_for("pdfa"), plain_pdf, flavour="2b")
    assert r.status_code == 200
    body = r.json()
    assert body["compliant"] is False and body["failed_rules"]


@pytest.mark.parametrize("level", ["2b", "3b"])
def test_converted_pdf_passes_validation(client, key_for, plain_pdf, level):
    headers = key_for("pdfa")
    converted = _post(client, "/v1/pdfa/convert", headers, plain_pdf, level=level)
    assert converted.status_code == 200
    assert converted.headers["content-type"] == "application/pdf"
    report = _post(client, "/v1/pdfa/validate", headers, converted.content).json()
    assert report["compliant"] is True, report
    assert level.upper()[0] in report["flavour"]


def test_non_pdf_rejected(client, key_for):
    r = _post(client, "/v1/pdfa/convert", key_for("pdfa"), b"GIF89a not a pdf")
    assert r.status_code == 415


def test_broken_pdf_gives_generic_error(client, key_for):
    r = _post(client, "/v1/pdfa/convert", key_for("pdfa"), b"%PDF-1.7 garbage")
    assert r.status_code == 422
    assert "gs" not in r.text.lower() and "ghostscript" not in r.text.lower()


def test_requires_pdfa_scope(client, key_for, plain_pdf):
    assert _post(client, "/v1/pdfa/validate", key_for("render"), plain_pdf).status_code == 403
