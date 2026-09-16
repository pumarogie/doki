from typing import Literal

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from docforge.auth import Principal, record_usage, require_product
from docforge.core import pdfa
from docforge.core.tools import ToolError
from docforge.uploads import page_count, read_pdf

router = APIRouter(prefix="/v1/pdfa", tags=["pdfa"])
guard = require_product("pdfa")


@router.post("/convert", response_class=Response, responses={200: {"content": {"application/pdf": {}}}})
async def convert(file: UploadFile, level: Literal["2b", "3b"] = Form("2b"), principal: Principal = Depends(guard)):
    pdf = await read_pdf(file)
    try:
        out = await run_in_threadpool(pdfa.convert, pdf, level)
    except ToolError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "could not convert this PDF")
    record_usage(principal, "pdfa", "convert", page_count(out))
    return Response(out, media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="pdfa.pdf"'})


@router.post("/validate")
async def validate(file: UploadFile, flavour: Literal["auto", "1b", "2b", "3b"] = Form("auto"), principal: Principal = Depends(guard)):
    pdf = await read_pdf(file)
    try:
        report = await run_in_threadpool(pdfa.validate, pdf, None if flavour == "auto" else flavour)
    except ToolError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "could not validate this PDF")
    record_usage(principal, "pdfa", "validate", page_count(pdf))
    return report
