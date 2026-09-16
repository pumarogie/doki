"""Stamps generated PDFs with the request/job id so a leaked file can be traced to the API key that made it.

Uses the standard XMP `xmpMM:DocumentID` (no custom XMP schema, so PDF/A stays valid) plus an Info entry.
"""
from io import BytesIO

import pikepdf

from docforge import logs

INFO_KEY = "/DocforgeTrace"


def stamp(pdf: bytes, trace: str | None = None) -> bytes:
    trace = trace or logs.request_id.get()
    if not trace:
        return pdf
    with pikepdf.open(BytesIO(pdf)) as doc:
        with doc.open_metadata(set_pikepdf_as_editor=False) as meta:
            meta["xmpMM:DocumentID"] = f"docforge:{trace}"
        doc.docinfo[INFO_KEY] = trace
        out = BytesIO()
        doc.save(out)
    return out.getvalue()


def read(pdf: bytes) -> str | None:
    with pikepdf.open(BytesIO(pdf)) as doc:
        value = doc.docinfo.get(INFO_KEY)
    return str(value) if value is not None else None
