import hashlib

from docforge.core import blobs
from docforge.db import connect, one


def put(organization_id: int, data: bytes, filename: str, content_type: str) -> int:
    """Store bytes content-addressed; returns the file id (deduplicated per organization).

    The row is written before the blob so the retention purge, which only deletes blobs that no
    row references, can never remove a blob a fresh upload is about to use.
    """
    sha = hashlib.sha256(data).hexdigest()
    with connect() as conn:
        file_id = one(
            conn,
            """INSERT INTO file (organization_id, sha256, filename, content_type, size_bytes)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (organization_id, sha256) DO UPDATE SET filename = file.filename
               RETURNING id""",
            (organization_id, sha, filename[:255], content_type, len(data)),
        )["id"]
    blobs.store.put(sha, data)
    return file_id


def delete_blob_if_unreferenced(sha256: str) -> bool:
    with connect() as conn:
        if conn.execute("SELECT 1 FROM file WHERE sha256 = %s LIMIT 1", (sha256,)).fetchone() is not None:
            return False
    blobs.store.delete(sha256)
    return True


def get(file_id: int, organization_id: int) -> tuple[dict, bytes] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, sha256, filename, content_type, size_bytes FROM file WHERE id = %s AND organization_id = %s",
            (file_id, organization_id),
        ).fetchone()
    if row is None:
        return None
    return row, blobs.store.get(row["sha256"])
