import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from docforge import db, idempotency, logs, observability
from docforge.auth import Principal, any_key
from docforge.core import jobs, storage
from docforge.products import einvoice, extract, pdfa, render, reports

log = logging.getLogger("docforge")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logs.configure()
    observability.init_sentry("api")
    db.pool.open()
    yield
    db.pool.close()


app = FastAPI(title="docforge", lifespan=lifespan)
app.add_middleware(idempotency.IdempotencyMiddleware)
app.add_middleware(observability.MetricsMiddleware)
app.add_middleware(logs.RequestIdMiddleware)
app.include_router(observability.router)
app.include_router(pdfa.router)
app.include_router(einvoice.router)
app.include_router(render.router)
app.include_router(extract.router)
app.include_router(reports.router)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    # Never leak internals (engine, model, stack) to clients.
    rid = logs.request_id.get() or logs.new_id()
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse({"error": "internal_error", "request_id": rid}, status_code=500,
                        headers={"X-Request-ID": rid})


@app.get("/health")
def health():
    with db.connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: int, principal: Principal = Depends(any_key)):
    job = jobs.get(job_id, principal.organization_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job


@app.get("/v1/files/{file_id}", response_class=Response)
def get_file(file_id: int, principal: Principal = Depends(any_key)):
    found = storage.get(file_id, principal.organization_id)
    if found is None:
        raise HTTPException(404, "file not found")
    meta, data = found
    return Response(data, media_type=meta["content_type"],
                    headers={"Content-Disposition": f'attachment; filename="file-{meta["id"]}"'})


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
