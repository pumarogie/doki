import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from psycopg.errors import ForeignKeyViolation, UniqueViolation
from pydantic import BaseModel, Field, StringConstraints

from docforge.auth import Principal, record_usage, require_product
from docforge.core import render as engine
from docforge.core.tools import ToolError
from docforge.db import connect
from docforge.uploads import page_count

router = APIRouter(prefix="/v1", tags=["render"])
guard = require_product("render")

MAX_SOURCE = 200_000
MAX_DATA_BYTES = 1_000_000

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Source = Annotated[str, StringConstraints(min_length=1, max_length=MAX_SOURCE)]
Engine = Literal["typst", "html"]


class TemplateIn(BaseModel):
    name: Name
    engine: Engine
    source: Source


class RenderIn(BaseModel):
    template_id: int | None = None
    template_name: Name | None = None
    engine: Engine | None = None
    source: Source | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    pdfa: bool = False


@router.post("/render", response_class=Response, responses={200: {"content": {"application/pdf": {}}}})
async def render(body: RenderIn, principal: Principal = Depends(guard)):
    engine_name, source = _resolve(body, principal)
    if len(json.dumps(body.data)) > MAX_DATA_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "data too large")
    if body.pdfa and engine_name != "typst":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "pdfa output is only supported for typst templates")
    try:
        pdf = await run_in_threadpool(engine.render, engine_name, source, body.data, body.pdfa)
    except engine.TemplateError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"template_error": str(e)[:2000]})
    except ToolError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "could not render this document")
    record_usage(principal, "render", engine_name, page_count(pdf))
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="document.pdf"'})


def _resolve(body: RenderIn, principal: Principal) -> tuple[str, str]:
    by_ref = body.template_id is not None or body.template_name is not None
    inline = body.engine is not None or body.source is not None
    if by_ref == inline:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "give either template_id/template_name or engine+source")
    if inline:
        if body.engine is None or body.source is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "inline rendering needs both engine and source")
        return body.engine, body.source
    t = _get(principal, body.template_id, body.template_name)
    return t["engine"], t["source"]


@router.post("/templates", status_code=status.HTTP_201_CREATED)
def create_template(body: TemplateIn, principal: Principal = Depends(guard)):
    try:
        with connect() as conn:
            return conn.execute(
                """INSERT INTO template (organization_id, name, engine, source) VALUES (%s, %s, %s, %s)
                   RETURNING id, name, engine, created_at""",
                (principal.organization_id, body.name, body.engine, body.source),
            ).fetchone()
    except UniqueViolation:
        raise HTTPException(status.HTTP_409_CONFLICT, "a template with this name already exists")


@router.get("/templates")
def list_templates(principal: Principal = Depends(guard)):
    with connect() as conn:
        return conn.execute(
            "SELECT id, name, engine, created_at FROM template WHERE organization_id = %s ORDER BY name",
            (principal.organization_id,),
        ).fetchall()


@router.get("/templates/{template_id}")
def get_template(template_id: int, principal: Principal = Depends(guard)):
    return _get(principal, template_id, None)


@router.put("/templates/{template_id}")
def update_template(template_id: int, body: TemplateIn, principal: Principal = Depends(guard)):
    try:
        with connect() as conn:
            row = conn.execute(
                """UPDATE template SET name = %s, engine = %s, source = %s
                   WHERE id = %s AND organization_id = %s RETURNING id, name, engine, created_at""",
                (body.name, body.engine, body.source, template_id, principal.organization_id),
            ).fetchone()
    except UniqueViolation:
        raise HTTPException(status.HTTP_409_CONFLICT, "a template with this name already exists")
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "template not found")
    return row


@router.delete("/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(template_id: int, principal: Principal = Depends(guard)):
    try:
        with connect() as conn:
            deleted = conn.execute(
                "DELETE FROM template WHERE id = %s AND organization_id = %s", (template_id, principal.organization_id)
            ).rowcount
    except ForeignKeyViolation:
        raise HTTPException(status.HTTP_409_CONFLICT, "template is used by a schedule")
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "template not found")


def _get(principal: Principal, template_id: int | None, name: str | None) -> dict:
    with connect() as conn:
        row = conn.execute(
            """SELECT id, name, engine, source, created_at FROM template
               WHERE organization_id = %s AND (id = %s OR name = %s)""",
            (principal.organization_id, template_id, name),
        ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "template not found")
    return row
