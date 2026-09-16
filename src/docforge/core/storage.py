import hashlib
import os
from pathlib import Path

from docforge.config import settings
from docforge.db import connect, one


def _path(sha256: str) -> Path:
    return settings.storage_dir / sha256[:2] / sha256


def put(organization_id: int, data: bytes, filename: str, content_type: str) -> int:
    """Store bytes content-addressed on disk; returns the file id (deduplicated per organization)."""
    sha = hashlib.sha256(data).hexdigest()
    path = _path(sha)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    with connect() as conn:
        return one(
            conn,
            """INSERT INTO file (organization_id, sha256, filename, content_type, size_bytes)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (organization_id, sha256) DO UPDATE SET filename = file.filename
               RETURNING id""",
            (organization_id, sha, filename[:255], content_type, len(data)),
        )["id"]


def get(file_id: int, organization_id: int) -> tuple[dict, bytes] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, sha256, filename, content_type, size_bytes FROM file WHERE id = %s AND organization_id = %s",
            (file_id, organization_id),
        ).fetchone()
    if row is None:
        return None
    return row, _path(row["sha256"]).read_bytes()
