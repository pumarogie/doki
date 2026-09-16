from io import BytesIO

from fastapi import HTTPException, UploadFile, status
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from doki.config import settings


async def read_upload(upload: UploadFile, magic: bytes | None = None) -> bytes:
    data = await upload.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "file too large")
    if magic is not None and not data.lstrip()[: len(magic)] == magic:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "unexpected file type")
    return data


async def read_pdf(upload: UploadFile) -> bytes:
    """Read an upload that must be a parseable PDF with at least one page."""
    return ensure_pdf(await read_upload(upload, magic=b"%PDF-"))


def ensure_pdf(data: bytes) -> bytes:
    if page_count(data) == 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unreadable PDF")
    return data


def page_count(pdf: bytes) -> int:
    try:
        return len(PdfReader(BytesIO(pdf)).pages)
    except (PdfReadError, ValueError, KeyError, TypeError):
        return 0
