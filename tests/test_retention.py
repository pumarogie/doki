from docforge.core import blobs, jobs, retention, storage
from docforge.db import connect, one


def _age(table: str, column: str, row_id: int, days: int) -> None:
    with connect() as conn:
        match (table, column):
            case ("file", "created_at"):
                conn.execute("UPDATE file SET created_at = now() - make_interval(days => %s) WHERE id = %s", (days, row_id))
            case ("job", "finished_at"):
                conn.execute("UPDATE job SET finished_at = now() - make_interval(days => %s) WHERE id = %s", (days, row_id))
            case ("usage_event", "created_at"):
                conn.execute("UPDATE usage_event SET created_at = now() - make_interval(days => %s) WHERE id = %s", (days, row_id))
            case _:
                raise ValueError((table, column))


def _blob_exists(sha: str) -> bool:
    try:
        blobs.store.get(sha)
    except FileNotFoundError:
        return False
    return True


def _sha(file_id: int) -> str:
    with connect() as conn:
        return one(conn, "SELECT sha256 FROM file WHERE id = %s", (file_id,))["sha256"]


def test_old_unreferenced_file_and_blob_are_removed(org_id):
    fid = storage.put(org_id, b"%PDF-old-unreferenced", "a.pdf", "application/pdf")
    sha = _sha(fid)
    _age("file", "created_at", fid, 40)
    assert retention.purge(retention_days=30)["files"] >= 1
    assert storage.get(fid, org_id) is None
    assert not _blob_exists(sha)


def test_recent_file_is_kept(org_id):
    fid = storage.put(org_id, b"%PDF-recent", "b.pdf", "application/pdf")
    retention.purge(retention_days=30)
    assert storage.get(fid, org_id) is not None


def test_blob_shared_with_another_org_survives(org_id):
    with connect() as conn:
        other = one(conn, "INSERT INTO organization (name) VALUES ('sharer') RETURNING id")["id"]
    data = b"%PDF-shared-content"
    old = storage.put(org_id, data, "c.pdf", "application/pdf")
    fresh = storage.put(other, data, "c.pdf", "application/pdf")
    _age("file", "created_at", old, 40)
    retention.purge(retention_days=30)
    assert storage.get(old, org_id) is None
    found = storage.get(fresh, other)
    assert found is not None and found[1] == data


def test_old_finished_job_goes_then_its_file(client, key_for, org_id):
    headers = key_for("extract")
    with connect() as conn:
        key_id = one(conn, "SELECT id FROM api_key ORDER BY id DESC LIMIT 1")["id"]
    fid = storage.put(org_id, b"%PDF-job-input", "d.pdf", "application/pdf")
    job_id = jobs.enqueue(key_id, "extract", "noop", {}, input_file_id=fid)
    jobs.fail({"id": job_id, "attempts": 99}, "x", permanent=True)
    _age("file", "created_at", fid, 40)

    _age("job", "finished_at", job_id, 5)  # job still recent -> file must stay
    retention.purge(retention_days=30)
    assert storage.get(fid, org_id) is not None

    _age("job", "finished_at", job_id, 40)
    retention.purge(retention_days=30)
    assert client.get(f"/v1/jobs/{job_id}", headers=headers).status_code == 404
    assert storage.get(fid, org_id) is None


def test_queued_job_keeps_its_file(org_id):
    with connect() as conn:
        key_id = one(conn, "SELECT id FROM api_key ORDER BY id DESC LIMIT 1")["id"]
    fid = storage.put(org_id, b"%PDF-queued-input", "e.pdf", "application/pdf")
    jobs.enqueue(key_id, "extract", "noop-queued", {}, input_file_id=fid)
    _age("file", "created_at", fid, 400)
    retention.purge(retention_days=30)
    assert storage.get(fid, org_id) is not None


def test_usage_kept_for_a_billing_year(key_for):
    key_for("pdfa")
    with connect() as conn:
        key_id = one(conn, "SELECT id FROM api_key ORDER BY id DESC LIMIT 1")["id"]
        recent = one(conn, "INSERT INTO usage_event (api_key_id, product, operation) VALUES (%s, 'pdfa', 'x') RETURNING id", (key_id,))["id"]
        ancient = one(conn, "INSERT INTO usage_event (api_key_id, product, operation) VALUES (%s, 'pdfa', 'x') RETURNING id", (key_id,))["id"]
    _age("usage_event", "created_at", recent, 200)
    _age("usage_event", "created_at", ancient, 500)
    retention.purge(usage_months=13)
    with connect() as conn:
        ids = {r["id"] for r in conn.execute("SELECT id FROM usage_event WHERE id IN (%s, %s)", (recent, ancient))}
    assert ids == {recent}
