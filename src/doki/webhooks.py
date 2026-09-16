"""Signed job-finished webhooks.

Signature header: `Doki-Signature: t=<unix ts>,v1=<hex hmac_sha256(secret, "<ts>.<raw body>")>`.
Receivers should recompute it and reject timestamps older than a few minutes.
"""
import hashlib
import hmac
import json
import logging
import os
import secrets as pysecrets
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from doki.auth import Principal, any_key
from doki.core import deliver, jobs, secrets
from doki.core.jobs import PermanentError
from doki.db import connect, one

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])

MAX_ENDPOINTS = 10


class EndpointIn(BaseModel):
    url: str = Field(min_length=8, max_length=2000)


def _check_url(url: str) -> None:
    parts = urlsplit(url)
    allow_http = os.environ.get("DELIVERY_ALLOW_PRIVATE") == "1"
    if parts.scheme not in (("https", "http") if allow_http else ("https",)) or not parts.hostname:
        raise deliver.DeliveryError("webhook URL must be https://")
    if parts.username or parts.password:
        raise deliver.DeliveryError("webhook URL must not contain credentials")
    deliver.check_public_host(parts.hostname)


@router.post("", status_code=status.HTTP_201_CREATED)
def create(body: EndpointIn, principal: Principal = Depends(any_key)):
    try:
        _check_url(body.url)
    except deliver.DeliveryError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    secret = f"whsec_{pysecrets.token_urlsafe(32)}"
    with connect() as conn:
        count = one(conn, "SELECT count(*) AS n FROM webhook_endpoint WHERE organization_id = %s",
                    (principal.organization_id,))["n"]
        if count >= MAX_ENDPOINTS:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"at most {MAX_ENDPOINTS} webhook endpoints")
        row = one(conn, """INSERT INTO webhook_endpoint (organization_id, url, secret_enc) VALUES (%s, %s, %s)
                           RETURNING id, url, created_at""",
                  (principal.organization_id, body.url, secrets.encrypt(secret)))
    return {**row, "secret": secret}  # shown once


@router.get("")
def list_endpoints(principal: Principal = Depends(any_key)):
    with connect() as conn:
        return conn.execute("SELECT id, url, created_at FROM webhook_endpoint WHERE organization_id = %s ORDER BY id",
                            (principal.organization_id,)).fetchall()


@router.delete("/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(endpoint_id: int, principal: Principal = Depends(any_key)):
    with connect() as conn:
        deleted = conn.execute("DELETE FROM webhook_endpoint WHERE id = %s AND organization_id = %s",
                               (endpoint_id, principal.organization_id)).rowcount
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "webhook endpoint not found")


def emit(job: dict, outcome: str) -> None:
    """Queue one delivery per endpoint of the job's organization."""
    with connect() as conn:
        row = one(
            conn,
            """SELECT j.id, j.product, j.operation, j.status, j.error, j.output_file_id, j.finished_at,
                      k.organization_id
               FROM job j JOIN api_key k ON k.id = j.api_key_id WHERE j.id = %s""",
            (job["id"],),
        )
        endpoints = conn.execute("SELECT id FROM webhook_endpoint WHERE organization_id = %s",
                                 (row["organization_id"],)).fetchall()
    event = {
        "id": f"evt_{pysecrets.token_hex(12)}",
        "type": "job.succeeded" if outcome == "done" else "job.failed",
        "created": int(time.time()),
        "data": {"job_id": row["id"], "product": row["product"], "operation": row["operation"],
                 "status": row["status"], "error": row["error"], "output_file_id": row["output_file_id"]},
    }
    for endpoint in endpoints:
        jobs.enqueue(job["api_key_id"], "webhooks", "deliver", {"endpoint_id": endpoint["id"], "event": event})


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # a redirect could point into our own network


_opener = urllib.request.build_opener(_NoRedirect)


# Receivers go down for deploys: 8 attempts back off from 30s to about 1h, roughly 2 hours in total.
@jobs.handler("webhooks", "deliver", max_attempts=8)
def deliver_event(job: dict) -> dict:
    params = job["params"]
    with connect() as conn:
        endpoint = conn.execute("SELECT url, secret_enc FROM webhook_endpoint WHERE id = %s",
                                (params["endpoint_id"],)).fetchone()
    if endpoint is None:
        raise PermanentError("webhook endpoint was deleted")
    try:
        _check_url(endpoint["url"])  # DNS may have changed since the endpoint was created
    except deliver.DeliveryError as e:
        raise PermanentError(str(e)) from e
    body = json.dumps(params["event"], separators=(",", ":")).encode()
    timestamp = int(time.time())
    request = urllib.request.Request(endpoint["url"], data=body, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": "doki-webhooks/1",
        "Doki-Signature": sign(secrets.decrypt(endpoint["secret_enc"]), timestamp, body),
        "Doki-Event-Id": params["event"]["id"],
    })
    try:
        with _opener.open(request, timeout=10) as response:
            code = response.status
    except urllib.error.HTTPError as e:
        code = e.code
    # URLError / timeouts propagate: retried with backoff.
    if 200 <= code < 300:
        return {"delivered": True, "status_code": code}
    if code in (408, 429) or code >= 500:
        raise ConnectionError(f"webhook receiver answered {code}")  # retried
    raise PermanentError(f"webhook receiver rejected the event with HTTP {code}")
