import os
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path

import facturx
from pypdf import PdfReader

from docforge.core import pdfa
from docforge.core.tools import ToolError, run

KOSIT_DIR = Path(os.environ.get("KOSIT_DIR", Path(__file__).resolve().parents[3] / "vendor" / "kosit"))
JAVA = os.environ.get("JAVA_BIN", "java")

# Attachment names used by ZUGFeRD 1/2, Factur-X and XRechnung-in-PDF.
EMBEDDED_NAMES = ("factur-x.xml", "zugferd-invoice.xml", "ZUGFeRD-invoice.xml", "xrechnung.xml")

NS = {
    "rep": "http://www.xoev.de/de/validator/varl/1",
    "s": "http://www.xoev.de/de/validator/framework/1/scenarios",
}


class NoInvoiceData(ValueError):
    """The PDF has no embedded e-invoice XML."""


def embedded_xml(pdf: bytes) -> bytes:
    attachments = PdfReader(BytesIO(pdf)).attachments
    for name in EMBEDDED_NAMES:
        if name in attachments:
            return attachments[name][0]
    raise NoInvoiceData("no embedded invoice XML found")


def validate(data: bytes, timeout: float = 120) -> dict:
    """Validate an XRechnung/EN16931 invoice (UBL or CII XML, or a PDF with embedded XML) with KoSIT."""
    source = "pdf" if data.lstrip().startswith(b"%PDF-") else "xml"
    xml = embedded_xml(data) if source == "pdf" else data
    daemon = os.environ.get("KOSIT_URL")
    report = _via_daemon(daemon, xml, timeout) if daemon else _via_cli(xml, timeout)
    return {"source": source, **_summarize(report)}


def _via_daemon(url: str, xml: bytes, timeout: float) -> bytes:
    """KoSIT daemon mode: POST the XML, get the same report XML the CLI writes. ~50ms instead of ~4s."""
    req = urllib.request.Request(url.rstrip("/") + "/", data=xml, method="POST",
                                 headers={"Content-Type": "application/xml"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        # The daemon answers 406 for rejected invoices but still returns the report.
        body = e.read()
        if e.code in (406, 422) and body.lstrip().startswith(b"<"):
            return body
        raise ToolError(f"KoSIT daemon returned HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ToolError(f"KoSIT daemon unreachable: {e}") from e


def _via_cli(xml: bytes, timeout: float) -> bytes:
    """Fallback for local development without the daemon: one JVM per call."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "invoice.xml")
        src.write_bytes(xml)
        # Exit code is non-zero for rejected invoices; the report is the source of truth.
        run([JAVA, "-jar", str(KOSIT_DIR / "validator.jar"),
             "-s", str(KOSIT_DIR / "config" / "scenarios.xml"), "-r", str(KOSIT_DIR / "config"),
             "-o", tmp, str(src)], timeout=timeout, ok_codes=range(256))
        report = Path(tmp, "invoice-report.xml")
        if not report.is_file():
            raise ToolError("KoSIT produced no report")
        return report.read_bytes()


def _summarize(report_xml: bytes) -> dict:
    root = ET.fromstring(report_xml)
    scenario = root.find("rep:scenarioMatched/s:scenario/s:name", NS)
    found = {"error": [], "warning": [], "information": []}
    for msg in root.iter(f"{{{NS['rep']}}}message"):
        found.get(msg.get("level") or "error", found["error"]).append({
            "rule": msg.get("code") or "schema",
            "message": " ".join((msg.text or "").split()),
            "location": msg.get("xpathLocation"),
        })
    return {
        "valid": root.get("valid") == "true",
        "scenario": scenario.text if scenario is not None else None,
        "errors": found["error"],
        "warnings": found["warning"],
        "notes": found["information"],
    }


def make_zugferd(pdf: bytes, xml: bytes) -> bytes:
    """Embed CII invoice XML into a PDF/A-3b, producing a ZUGFeRD / Factur-X hybrid invoice."""
    base = pdfa.convert(pdf, level="3b")
    try:
        return facturx.generate_from_binary(base, xml, check_xsd=True)
    except Exception as e:  # factur-x raises bare Exception for invalid XML
        raise ValueError(f"invoice XML rejected: {e}") from e
