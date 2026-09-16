from fastapi import Depends

from docforge import db
from docforge.auth import Principal, record_usage, require_product
from docforge.main import app

pdfa_guard = require_product("pdfa")


@app.get("/_test/pdfa")
def _probe(principal: Principal = Depends(pdfa_guard)):
    record_usage(principal, "pdfa", "probe", page_count=3)
    return {"ok": True}


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_missing_and_invalid_key_rejected(client):
    assert client.get("/_test/pdfa").status_code == 401
    assert client.get("/_test/pdfa", headers={"Authorization": "Bearer df_nope"}).status_code == 401


def test_scope_enforced(client, key_for):
    assert client.get("/_test/pdfa", headers=key_for("extract")).status_code == 403
    assert client.get("/_test/pdfa", headers=key_for("pdfa")).status_code == 200


def test_revoked_key_rejected(client, key_for):
    headers = key_for("pdfa")
    prefix = headers["Authorization"].split("_")[1]
    with db.connect() as conn:
        conn.execute("UPDATE api_key SET revoked_at = now() WHERE prefix = %s", (prefix,))
    assert client.get("/_test/pdfa", headers=headers).status_code == 401


def test_usage_is_recorded_per_org(client, key_for):
    headers = key_for("pdfa")
    client.get("/_test/pdfa", headers=headers)
    client.get("/_test/pdfa", headers=headers)
    usage = client.get("/v1/usage", headers=headers).json()["usage"]
    probe = next(u for u in usage if u["operation"] == "probe")
    assert probe["calls"] >= 2 and probe["pages"] >= 6


def test_key_is_stored_hashed(key_for):
    key = key_for("pdfa")["Authorization"].removeprefix("Bearer ")
    with db.connect() as conn:
        hashes = [r["key_hash"] for r in conn.execute("SELECT key_hash FROM api_key")]
    assert key not in hashes


def test_rate_limit_returns_429_with_retry_after(client, key_for):
    headers = key_for("pdfa", rate_per_minute=3)
    for _ in range(3):
        assert client.get("/_test/pdfa", headers=headers).status_code == 200
    r = client.get("/_test/pdfa", headers=headers)
    assert r.status_code == 429
    assert 1 <= int(r.headers["Retry-After"]) <= 60
    # a different key is unaffected
    assert client.get("/_test/pdfa", headers=key_for("pdfa")).status_code == 200


def test_monthly_page_quota_gives_402(client, key_for):
    headers = key_for("pdfa", monthly_pages=5)
    assert client.get("/_test/pdfa", headers=headers).status_code == 200  # 3 pages used
    r = client.get("/_test/pdfa", headers=headers)  # would be 6 > 5
    assert r.status_code == 402
    assert client.get("/v1/usage", headers=headers).json()["usage"][0]["pages"] == 3


def test_revoke_key_by_prefix(client, key_for):
    from docforge.auth import revoke_key

    headers = key_for("pdfa")
    prefix = headers["Authorization"].split("_")[1]
    assert revoke_key(prefix) is True
    assert revoke_key(prefix) is False
    assert client.get("/_test/pdfa", headers=headers).status_code == 401
