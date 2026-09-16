"""Idempotency-Key support for POST requests (Stripe-style).

The first request with a key runs normally and its response is stored. A retry with the same key and
the same request replays the stored response without doing (or billing) the work again.
"""
import hashlib
import json

from docforge.auth import hash_key
from docforge.core import storage
from docforge.db import connect, one

STALE_IN_PROGRESS = "10 minutes"
MAX_KEY_LENGTH = 255


def _json(status: int, body: dict) -> tuple[int, bytes, str]:
    return status, json.dumps({"detail": body}).encode(), "application/json"


class IdempotencyMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers") or []) if scope["type"] == "http" else {}
        key = headers.get(b"idempotency-key", b"").decode("latin-1").strip()
        auth = headers.get(b"authorization", b"").decode("latin-1")
        if scope["type"] != "http" or scope["method"] != "POST" or not key or not auth.lower().startswith("bearer "):
            await self.app(scope, receive, send)
            return
        if len(key) > MAX_KEY_LENGTH:
            await self._respond(send, *_json(422, {"error": "idempotency_key_too_long"}))
            return

        with connect() as conn:
            owner = conn.execute(
                "SELECT id, organization_id FROM api_key WHERE key_hash = %s AND revoked_at IS NULL",
                (hash_key(auth[7:].strip()),),
            ).fetchone()
        if owner is None:  # let auth produce the 401
            await self.app(scope, receive, send)
            return

        body = await _read_body(receive)
        request_hash = fingerprint(scope, headers.get(b"content-type", b""), body)

        state = _claim(owner["id"], key, request_hash)
        if state == "mismatch":
            await self._respond(send, *_json(422, {"error": "idempotency_key_reused",
                                                    "message": "this key was used with a different request"}))
            return
        if state == "in_progress":
            await self._respond(send, *_json(409, {"error": "request_in_progress",
                                                    "message": "a request with this key is still running"}))
            return
        if isinstance(state, dict):
            data = b""
            if state["response_file_id"] is not None:
                found = storage.get(state["response_file_id"], owner["organization_id"])
                data = found[1] if found else b""
            await self._respond(send, state["status_code"], data, state["content_type"] or "application/octet-stream",
                                replayed=True)
            return

        captured: dict = {"status": 500, "headers": [], "body": bytearray()}

        async def replay_receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def capture_send(message):
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
                captured["headers"] = message.get("headers", [])
            elif message["type"] == "http.response.body":
                captured["body"] += message.get("body", b"")
            await send(message)

        try:
            await self.app(scope, replay_receive, capture_send)
        finally:
            _finish(owner, key, captured)

    @staticmethod
    async def _respond(send, status: int, body: bytes, content_type: str, replayed: bool = False) -> None:
        headers = [(b"content-type", content_type.encode()), (b"content-length", str(len(body)).encode())]
        if replayed:
            headers.append((b"idempotent-replayed", b"true"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def fingerprint(scope, content_type: bytes, body: bytes) -> str:
    """Hash of the request. Multipart boundaries are random per attempt, so they are normalised away."""
    media_type, _, params = content_type.partition(b";")
    boundary = next((p.split(b"=", 1)[1].strip(b' "') for p in params.split(b";") if p.strip().startswith(b"boundary=")), b"")
    if boundary:
        body = body.replace(b"--" + boundary, b"--BOUNDARY")
    digest = hashlib.sha256()
    digest.update(f"{scope['method']} {scope['path']}?{scope.get('query_string', b'').decode()}\n".encode())
    digest.update(media_type.strip() + b"\n" + body)
    return digest.hexdigest()


async def _read_body(receive) -> bytes:
    chunks = []
    while True:
        message = await receive()
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            return b"".join(chunks)


def _claim(api_key_id: int, key: str, request_hash: str) -> str | dict | None:
    """None = we own the key now; 'mismatch' / 'in_progress' / completed row otherwise."""
    with connect() as conn, conn.transaction():
        inserted = conn.execute(
            """INSERT INTO idempotency_key (api_key_id, key, request_hash) VALUES (%s, %s, %s)
               ON CONFLICT DO NOTHING RETURNING key""",
            (api_key_id, key, request_hash),
        ).fetchone()
        if inserted is not None:
            return None
        row = one(
            conn,
            """SELECT request_hash, status_code, content_type, response_file_id, completed_at,
                      created_at < now() - %s::interval AS stale
               FROM idempotency_key WHERE api_key_id = %s AND key = %s FOR UPDATE""",
            (STALE_IN_PROGRESS, api_key_id, key),
        )
        if row["request_hash"] != request_hash:
            return "mismatch"
        if row["completed_at"] is not None:
            return row
        if row["stale"]:  # the original request died without finishing: take the key over
            conn.execute("UPDATE idempotency_key SET created_at = now() WHERE api_key_id = %s AND key = %s",
                         (api_key_id, key))
            return None
        return "in_progress"


def _finish(owner: dict, key: str, captured: dict) -> None:
    status = captured["status"]
    with connect() as conn:
        if status >= 500 or status == 429:
            # Retryable failures must not be replayed: release the key.
            conn.execute("DELETE FROM idempotency_key WHERE api_key_id = %s AND key = %s", (owner["id"], key))
            return
    content_type = next((v.decode("latin-1") for k, v in captured["headers"] if k.lower() == b"content-type"),
                        "application/octet-stream")
    file_id = storage.put(owner["organization_id"], bytes(captured["body"]), f"idempotent-{key}", content_type)
    with connect() as conn:
        conn.execute(
            """UPDATE idempotency_key SET status_code = %s, content_type = %s, response_file_id = %s,
                      completed_at = now() WHERE api_key_id = %s AND key = %s""",
            (status, content_type, file_id, owner["id"], key),
        )
