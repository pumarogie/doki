import logging
from datetime import datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException, status
from psycopg.types.json import Jsonb
from pydantic import BaseModel, EmailStr, Field, StringConstraints, field_validator

from docforge.auth import Principal, QuotaExceeded, charge, require_product
from docforge.core import deliver, jobs, render, secrets, storage, watermark
from docforge.core.jobs import PermanentError
from docforge.db import connect, one
from docforge.uploads import page_count

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/schedules", tags=["reports"])
guard = require_product("reports")

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class EmailDelivery(BaseModel):
    type: Literal["email"]
    to: list[EmailStr] = Field(min_length=1, max_length=10)
    subject: Annotated[str, StringConstraints(max_length=200)] = "Your scheduled report"


class SftpDelivery(BaseModel):
    type: Literal["sftp"]
    host: Annotated[str, StringConstraints(min_length=1, max_length=253)]
    port: int = Field(22, ge=1, le=65535)
    username: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    password: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    host_key: Annotated[str, StringConstraints(min_length=20, max_length=2000)]
    directory: Annotated[str, StringConstraints(max_length=500)] = "."


class ScheduleIn(BaseModel):
    name: Name
    template_id: int
    cron: str
    timezone: str = "UTC"
    data: dict[str, Any] = Field(default_factory=dict)
    delivery: EmailDelivery | SftpDelivery = Field(discriminator="type")
    enabled: bool = True

    @field_validator("cron")
    @classmethod
    def valid_cron(cls, v: str) -> str:
        if len(v.split()) != 5 or not croniter.is_valid(v):
            raise ValueError("cron must be a 5-field expression like '0 7 * * 1-5'")
        return v

    @field_validator("timezone")
    @classmethod
    def valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("unknown IANA timezone")
        return v


def next_run(cron: str, tz: str, after: datetime | None = None) -> datetime:
    base = (after or datetime.now(ZoneInfo("UTC"))).astimezone(ZoneInfo(tz))
    return croniter(cron, base).get_next(datetime)


def _stored_delivery(d: EmailDelivery | SftpDelivery) -> dict:
    data = d.model_dump()
    if isinstance(d, SftpDelivery):
        data["password_enc"] = secrets.encrypt(data.pop("password"))
    return data


def _public(row: dict) -> dict:
    delivery = {k: v for k, v in row["delivery"].items() if k != "password_enc"}
    return {**row, "delivery": delivery}


_COLUMNS = "s.id, s.name, s.template_id, s.cron, s.timezone, s.data, s.delivery, s.next_run_at, s.enabled"


def _check_template(conn, principal: Principal, template_id: int) -> None:
    if conn.execute("SELECT 1 FROM template WHERE id = %s AND organization_id = %s",
                    (template_id, principal.organization_id)).fetchone() is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "template not found")


def _get(conn, principal: Principal, schedule_id: int) -> dict:
    row = conn.execute(
        f"""SELECT {_COLUMNS} FROM schedule s JOIN api_key k ON k.id = s.api_key_id
            WHERE s.id = %s AND k.organization_id = %s""",
        (schedule_id, principal.organization_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "schedule not found")
    return row


@router.post("", status_code=status.HTTP_201_CREATED)
def create(body: ScheduleIn, principal: Principal = Depends(guard)):
    with connect() as conn:
        _check_template(conn, principal, body.template_id)
        row = one(
            conn,
            """INSERT INTO schedule (api_key_id, name, template_id, cron, timezone, data, delivery, next_run_at, enabled)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (principal.api_key_id, body.name, body.template_id, body.cron, body.timezone, Jsonb(body.data),
             Jsonb(_stored_delivery(body.delivery)), next_run(body.cron, body.timezone), body.enabled),
        )
        return _public(_get(conn, principal, row["id"]))


@router.get("")
def list_schedules(principal: Principal = Depends(guard)):
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT {_COLUMNS} FROM schedule s JOIN api_key k ON k.id = s.api_key_id
                WHERE k.organization_id = %s ORDER BY s.name""",
            (principal.organization_id,),
        ).fetchall()
    return [_public(r) for r in rows]


@router.get("/{schedule_id}")
def get_schedule(schedule_id: int, principal: Principal = Depends(guard)):
    with connect() as conn:
        return _public(_get(conn, principal, schedule_id))


@router.put("/{schedule_id}")
def update(schedule_id: int, body: ScheduleIn, principal: Principal = Depends(guard)):
    with connect() as conn:
        _get(conn, principal, schedule_id)
        _check_template(conn, principal, body.template_id)
        conn.execute(
            """UPDATE schedule SET name = %s, template_id = %s, cron = %s, timezone = %s, data = %s,
                      delivery = %s, next_run_at = %s, enabled = %s WHERE id = %s""",
            (body.name, body.template_id, body.cron, body.timezone, Jsonb(body.data),
             Jsonb(_stored_delivery(body.delivery)), next_run(body.cron, body.timezone), body.enabled, schedule_id),
        )
        return _public(_get(conn, principal, schedule_id))


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(schedule_id: int, principal: Principal = Depends(guard)):
    with connect() as conn:
        _get(conn, principal, schedule_id)
        conn.execute("DELETE FROM schedule WHERE id = %s", (schedule_id,))


@router.post("/{schedule_id}/run", status_code=status.HTTP_202_ACCEPTED)
def run_now(schedule_id: int, principal: Principal = Depends(guard)):
    with connect() as conn:
        _get(conn, principal, schedule_id)
    job_id = jobs.enqueue(principal.api_key_id, "reports", "run_schedule", {"schedule_id": schedule_id})
    return {"job_id": job_id, "status": "queued"}


def enqueue_due(limit: int = 100) -> int:
    """Called by the worker every tick: turns due schedules into jobs and advances next_run_at."""
    with connect() as conn, conn.transaction():
        due = conn.execute(
            """SELECT s.id, s.api_key_id, s.cron, s.timezone FROM schedule s
               JOIN api_key k ON k.id = s.api_key_id
               WHERE s.enabled AND s.next_run_at <= now() AND k.revoked_at IS NULL
               ORDER BY s.next_run_at LIMIT %s FOR UPDATE OF s SKIP LOCKED""",
            (limit,),
        ).fetchall()
        for s in due:
            conn.execute(
                "INSERT INTO job (api_key_id, product, operation, params) VALUES (%s, 'reports', 'run_schedule', %s)",
                (s["api_key_id"], Jsonb({"schedule_id": s["id"]})),
            )
            # Runs missed while the worker was down collapse into this one catch-up run;
            # the next run is computed from now, so a long outage never floods the recipient.
            conn.execute("UPDATE schedule SET next_run_at = %s WHERE id = %s",
                         (next_run(s["cron"], s["timezone"]), s["id"]))
    return len(due)


@jobs.handler("reports", "run_schedule")
def run(job: dict) -> dict:
    with connect() as conn:
        s = conn.execute(
            """SELECT s.id, s.name, s.data, s.delivery, t.engine, t.source, k.organization_id, k.revoked_at
               FROM schedule s JOIN template t ON t.id = s.template_id JOIN api_key k ON k.id = s.api_key_id
               WHERE s.id = %s""",
            (job["params"]["schedule_id"],),
        ).fetchone()
    if s is None:
        raise PermanentError("schedule no longer exists")
    if s["revoked_at"] is not None:
        raise PermanentError("the API key that owns this schedule was revoked")
    try:
        pdf = watermark.stamp(render.render(s["engine"], s["source"], s["data"]))
    except render.TemplateError as e:
        raise PermanentError(f"template error: {str(e)[:500]}") from e

    filename = f"{s['name']}-{datetime.now(ZoneInfo('UTC')):%Y%m%d-%H%M}.pdf".replace("/", "_")
    file_id = storage.put(s["organization_id"], pdf, filename, "application/pdf")
    d = s["delivery"]
    try:
        if d["type"] == "email":
            deliver.email(d["to"], d["subject"], f"Attached: {s['name']}", filename, pdf)
            target = ", ".join(d["to"])
        else:
            target = deliver.sftp(d["host"], d["port"], d["username"], secrets.decrypt(d["password_enc"]),
                                  d["host_key"], d["directory"], filename, pdf)
    except deliver.DeliveryError as e:
        raise PermanentError(str(e)) from e

    try:
        charge(job["api_key_id"], "reports", "run_schedule", page_count(pdf))
    except QuotaExceeded as e:
        raise PermanentError("monthly page quota exceeded") from e
    return {"delivered_to": target, "output_file_id": file_id}
