import json
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status

from docforge.auth import Principal, require_product
from docforge.core import extract as engine
from docforge.core import jobs, llm, storage
from docforge.core.jobs import PermanentError
from docforge.db import connect, one
from docforge.uploads import page_count, read_pdf

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/extract", tags=["extract"])
guard = require_product("extract")

MAX_PDF_BYTES = 32 * 1024 * 1024  # model request limit
MAX_PAGES = 100
MAX_CUSTOM_FIELDS = 50


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def submit(
    file: UploadFile,
    schema: str = Form("invoice", description="invoice | delivery_note | contract | custom"),
    fields: str | None = Form(None, description='custom only: JSON object {"field_name": "description"}'),
    verify: bool = Form(False),
    principal: Principal = Depends(guard),
):
    field_map = _fields(schema, fields)
    pdf = await read_pdf(file)
    if len(pdf) > MAX_PDF_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "PDF too large for extraction (max 32 MB)")
    pages = page_count(pdf)
    if pages > MAX_PAGES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, f"PDF has too many pages (max {MAX_PAGES})")
    file_id = storage.put(principal.organization_id, pdf, file.filename or "document.pdf", "application/pdf")
    job_id = jobs.enqueue(principal.api_key_id, "extract", "extract",
                          # jsonb reorders object keys, so keep the requested field order as pairs
                          {"fields": list(field_map.items()), "verify": verify, "pages": pages},
                          input_file_id=file_id)
    return {"job_id": job_id, "status": "queued"}


def _fields(schema: str, fields: str | None) -> dict[str, str]:
    if schema != "custom":
        if schema not in engine.SCHEMAS:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown schema; use one of {sorted(engine.SCHEMAS)} or custom")
        return engine.SCHEMAS[schema]
    try:
        parsed = json.loads(fields or "")
    except json.JSONDecodeError:
        parsed = None
    if not (
        isinstance(parsed, dict) and 0 < len(parsed) <= MAX_CUSTOM_FIELDS
        and all(isinstance(k, str) and k.isidentifier() and len(k) <= 64 and isinstance(v, str) and len(v) <= 300
                for k, v in parsed.items())
    ):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            f"custom schema needs fields as a JSON object of up to {MAX_CUSTOM_FIELDS} identifier: description pairs")
    return parsed


@jobs.handler("extract", "extract")
def run(job: dict) -> dict:
    with connect() as conn:
        org_id = one(conn, "SELECT organization_id FROM api_key WHERE id = %s", (job["api_key_id"],))["organization_id"]
    found = storage.get(job["input_file_id"], org_id)
    if found is None:
        raise PermanentError("input file missing")
    _, pdf = found
    params = job["params"]
    try:
        result = engine.extract(pdf, dict(params["fields"]), verify=params["verify"])
    except llm.LLMError as e:
        log.warning("job %s: %s", job["id"], e)
        raise PermanentError("the document could not be processed") from e
    with connect() as conn:
        conn.execute(
            "INSERT INTO usage_event (api_key_id, product, operation, page_count) VALUES (%s, 'extract', 'extract', %s)",
            (job["api_key_id"], params["pages"]),
        )
    return result
