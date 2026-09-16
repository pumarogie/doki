"""Content-addressed blob stores: local disk (default) or any S3-compatible bucket (AWS, MinIO, R2, Hetzner)."""
import os
import uuid
from pathlib import Path
from typing import Protocol

from docforge.config import settings


class BlobStore(Protocol):
    def put(self, sha256: str, data: bytes) -> None: ...
    def get(self, sha256: str) -> bytes: ...
    def exists(self, sha256: str) -> bool: ...
    def delete(self, sha256: str) -> None: ...


def _key(sha256: str) -> str:
    return f"{sha256[:2]}/{sha256}"


class LocalBlobs:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, sha256: str) -> Path:
        return self.root / _key(sha256)

    def put(self, sha256: str, data: bytes) -> None:
        path = self._path(sha256)
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{sha256}.{uuid.uuid4().hex}.part")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def get(self, sha256: str) -> bytes:
        return self._path(sha256).read_bytes()

    def exists(self, sha256: str) -> bool:
        return self._path(sha256).exists()

    def delete(self, sha256: str) -> None:
        self._path(sha256).unlink(missing_ok=True)


class S3Blobs:
    def __init__(self, bucket: str, prefix: str = "", endpoint_url: str | None = None) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.client = boto3.client(
            "s3", endpoint_url=endpoint_url,
            config=Config(retries={"max_attempts": 5, "mode": "standard"}, s3={"addressing_style": "path"}),
        )

    def _k(self, sha256: str) -> str:
        return self.prefix + _key(sha256)

    def put(self, sha256: str, data: bytes) -> None:
        # Content-addressed, so overwriting with identical bytes is harmless; skip the HEAD round-trip.
        self.client.put_object(Bucket=self.bucket, Key=self._k(sha256), Body=data)

    def get(self, sha256: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self._k(sha256))["Body"].read()

    def exists(self, sha256: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self._k(sha256))
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
        return True

    def delete(self, sha256: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._k(sha256))


def from_env() -> BlobStore:
    if os.environ.get("STORAGE_BACKEND", "local") == "s3":
        return S3Blobs(os.environ["S3_BUCKET"], os.environ.get("S3_PREFIX", ""), os.environ.get("S3_ENDPOINT_URL") or None)
    return LocalBlobs(settings.storage_dir)


store: BlobStore = from_env()
