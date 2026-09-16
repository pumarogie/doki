import hashlib
import shutil
import socket
import subprocess
import time
import uuid

import pytest

from docforge.core import blobs


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def minio(monkeypatch_module):
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True, check=False).returncode != 0:
        pytest.skip("needs docker")
    port = _free_port()
    name = f"docforge-minio-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:9000",
                    "-e", "MINIO_ROOT_USER=testkey", "-e", "MINIO_ROOT_PASSWORD=testsecret123",
                    "quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z", "server", "/data"], check=True, capture_output=True)
    monkeypatch_module.setenv("AWS_ACCESS_KEY_ID", "testkey")
    monkeypatch_module.setenv("AWS_SECRET_ACCESS_KEY", "testsecret123")
    monkeypatch_module.setenv("AWS_DEFAULT_REGION", "us-east-1")
    endpoint = f"http://127.0.0.1:{port}"
    try:
        store = blobs.S3Blobs("docforge-test", endpoint)
        for _ in range(60):
            try:
                store.client.create_bucket(Bucket="docforge-test")
                break
            except Exception:  # noqa: BLE001  server still starting
                time.sleep(0.5)
        else:
            pytest.skip("minio did not start")
        yield store
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


@pytest.fixture(scope="module")
def monkeypatch_module():
    with pytest.MonkeyPatch.context() as mp:
        yield mp


@pytest.fixture(params=["local", "s3"])
def store(request, tmp_path):
    if request.param == "local":
        return blobs.LocalBlobs(tmp_path)
    return request.getfixturevalue("minio")


def test_roundtrip_and_delete(store):
    data = uuid.uuid4().bytes * 100
    sha = hashlib.sha256(data).hexdigest()
    store.put(sha, data)
    store.put(sha, data)  # idempotent
    assert store.get(sha) == data
    store.delete(sha)
    store.delete(sha)  # deleting a missing blob is fine
    with pytest.raises(Exception):  # noqa: B017  FileNotFoundError locally, NoSuchKey on S3
        store.get(sha)


def test_s3_keys_are_sharded_by_hash(minio):
    data = b"sharded"
    sha = hashlib.sha256(data).hexdigest()
    minio.put(sha, data)
    keys = [o["Key"] for o in minio.client.list_objects_v2(Bucket="docforge-test")["Contents"]]
    assert f"{sha[:2]}/{sha}" in keys


def test_app_storage_works_on_s3(org_id, minio, monkeypatch):
    from docforge.core import storage

    monkeypatch.setattr(blobs, "store", minio)
    fid = storage.put(org_id, b"%PDF-on-s3", "s3.pdf", "application/pdf")
    found = storage.get(fid, org_id)
    assert found is not None and found[1] == b"%PDF-on-s3"
