import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from docforge import db
from docforge.auth import Principal, any_key
from docforge.products import einvoice, pdfa, render

log = logging.getLogger("docforge")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.pool.open()
    yield
    db.pool.close()


app = FastAPI(title="docforge", lifespan=lifespan)
app.include_router(pdfa.router)
app.include_router(einvoice.router)
app.include_router(render.router)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    # Never leak internals (engine, model, stack) to clients.
    request_id = uuid.uuid4().hex
    log.exception("unhandled error request_id=%s path=%s", request_id, request.url.path)
    return JSONResponse({"error": "internal_error", "request_id": request_id}, status_code=500)


@app.get("/health")
def health():
    with db.connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/v1/usage")
def usage(principal: Principal = Depends(any_key)):
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT u.product, u.operation, count(*) AS calls, sum(u.page_count) AS pages
               FROM usage_event u JOIN api_key k ON k.id = u.api_key_id
               WHERE k.organization_id = %s AND u.created_at >= date_trunc('month', now())
               GROUP BY u.product, u.operation ORDER BY u.product, u.operation""",
            (principal.organization_id,),
        ).fetchall()
    return {"period": "current_month", "usage": rows}
