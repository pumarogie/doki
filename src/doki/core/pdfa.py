import json
import os
import tempfile
from pathlib import Path

from doki.core.tools import ToolError, run

LEVELS = {"2b": "2", "3b": "3"}

GS = os.environ.get("GS_BIN", "gs")
VERAPDF = os.environ.get("VERAPDF_BIN", "verapdf")
_ICC_CANDIDATES = [
    os.environ.get("PDFA_ICC_PROFILE", ""),
    "/opt/homebrew/share/ghostscript/iccprofiles/default_rgb.icc",
    "/usr/share/ghostscript/iccprofiles/default_rgb.icc",
    "/usr/share/color/icc/ghostscript/default_rgb.icc",
]

# Ghostscript needs a PostScript prologue that embeds an ICC profile as the PDF/A OutputIntent.
_DEF_PS = """%!
[/_objdef {{icc_PDFA}} /type /stream /OBJ pdfmark
[{{icc_PDFA}} <</N 3>> /PUT pdfmark
[{{icc_PDFA}} ({icc}) (r) file /PUT pdfmark
[/_objdef {{OutputIntent_PDFA}} /type /dict /OBJ pdfmark
[{{OutputIntent_PDFA}} <<
  /Type /OutputIntent /S /GTS_PDFA1
  /DestOutputProfile {{icc_PDFA}}
  /OutputConditionIdentifier (sRGB)
>> /PUT pdfmark
[{{Catalog}} <</OutputIntents [ {{OutputIntent_PDFA}} ]>> /PUT pdfmark
"""


def _icc_profile() -> str:
    for path in _ICC_CANDIDATES:
        if path and Path(path).is_file():
            return path
    raise ToolError("no RGB ICC profile found; set PDFA_ICC_PROFILE")


def _ps_string(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def convert(pdf: bytes, level: str = "2b", timeout: float = 120) -> bytes:
    if level not in LEVELS:
        raise ValueError(f"level must be one of {sorted(LEVELS)}")
    icc = _icc_profile()
    with tempfile.TemporaryDirectory() as tmp:
        src, out, prologue = Path(tmp, "in.pdf"), Path(tmp, "out.pdf"), Path(tmp, "pdfa_def.ps")
        src.write_bytes(pdf)
        prologue.write_text(_DEF_PS.format(icc=_ps_string(icc)))
        run([
            GS, "-q", "-dBATCH", "-dNOPAUSE", "-dNOOUTERSAVE",
            f"-dPDFA={LEVELS[level]}", "-dPDFACompatibilityPolicy=1",
            "-sDEVICE=pdfwrite", "-sColorConversionStrategy=RGB", "-sProcessColorModel=DeviceRGB",
            f"--permit-file-read={icc}",
            f"-sOutputFile={out}", str(prologue), str(src),
        ], timeout=timeout)
        return out.read_bytes()


def validate(pdf: bytes, flavour: str | None = None, timeout: float = 120) -> dict:
    """Run veraPDF. flavour=None auto-detects from the file's metadata."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "in.pdf")
        src.write_bytes(pdf)
        # veraPDF exits 1 when the file is not compliant; that's a result, not a failure.
        proc = run([VERAPDF, "--format", "json", "--flavour", flavour or "0", str(src)], timeout=timeout, ok_codes=(0, 1))
    return _summarize(json.loads(proc.stdout))


def _summarize(report: dict) -> dict:
    job = report["report"]["jobs"][0]
    result = job.get("validationResult")
    if isinstance(result, list):
        result = result[0] if result else None
    if not result:
        return {"compliant": False, "flavour": None, "failed_rules": [], "error": "not a PDF/A candidate or unreadable"}
    failed = [
        {"clause": r.get("clause"), "test": r.get("testNumber"), "description": r.get("description"), "failed_checks": r.get("failedChecks")}
        for r in result.get("details", {}).get("ruleSummaries", [])
        if r.get("ruleStatus") == "FAILED"
    ]
    return {"compliant": bool(result.get("compliant")), "flavour": result.get("profileName"), "failed_rules": failed}
