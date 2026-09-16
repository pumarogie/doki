from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from doki.auth import Principal, record_usage, require_product
from doki.core import einvoice, watermark
from doki.core.tools import ToolError
from doki.uploads import ensure_pdf, page_count, read_pdf, read_upload

router = APIRouter(prefix="/v1/einvoice", tags=["einvoice"])
guard = require_product("einvoice")


@router.post("/validate")
async def validate(file: UploadFile, principal: Principal = Depends(guard)):
    """Validate an XRechnung / EN16931 invoice: UBL or CII XML, or a ZUGFeRD/Factur-X PDF."""
    data = await read_upload(file)
    head = data.lstrip()[:5]
    if head == b"%PDF-":
        ensure_pdf(data)
    elif not head.startswith(b"<"):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "expected invoice XML or PDF")
    try:
        report = await run_in_threadpool(einvoice.validate, data)
    except einvoice.NoInvoiceData:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "PDF contains no embedded invoice XML")
    except ToolError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "could not validate this invoice")
    record_usage(principal, "einvoice", "validate")
    return report


@router.post("/zugferd", response_class=Response, responses={200: {"content": {"application/pdf": {}}}})
async def zugferd(pdf: UploadFile, xml: UploadFile, principal: Principal = Depends(guard)):
    """Combine a visual invoice PDF and CII XML into a PDF/A-3 ZUGFeRD / Factur-X invoice."""
    pdf_bytes = await read_pdf(pdf)
    xml_bytes = await read_upload(xml, magic=b"<")
    try:
        out = await run_in_threadpool(einvoice.make_zugferd, pdf_bytes, xml_bytes)
        out = await run_in_threadpool(watermark.stamp, out)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invoice XML is not valid CII for ZUGFeRD")
    except ToolError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "could not build this invoice")
    record_usage(principal, "einvoice", "zugferd", page_count(out))
    return Response(out, media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="invoice.pdf"'})
