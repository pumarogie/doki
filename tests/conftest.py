import os
import tempfile

from cryptography.fernet import Fernet

os.environ.setdefault("DATABASE_URL", "postgresql://docforge_app@localhost/docforge_test")
os.environ["LLM_PROVIDER"] = "mock"
os.environ["DELIVERY_ALLOW_PRIVATE"] = "1"
os.environ.setdefault("DOCFORGE_SECRET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("STORAGE_DIR", tempfile.mkdtemp(prefix="docforge-test-"))
OWNER_URL = os.environ.setdefault("TEST_OWNER_DATABASE_URL", "postgresql://docforge_owner@localhost/docforge_test")

import psycopg
import pytest
from fastapi.testclient import TestClient

from docforge import db
from docforge.auth import create_key
from docforge.db import one
from docforge.main import app


@pytest.fixture(scope="session", autouse=True)
def fresh_schema():
    with psycopg.connect(OWNER_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public; GRANT USAGE ON SCHEMA public TO docforge_app")
    db.migrate(OWNER_URL)
    yield


@pytest.fixture(scope="session")
def client(fresh_schema):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def org_id(client):
    with db.connect() as conn:
        return one(conn, "INSERT INTO organization (name) VALUES ('test') RETURNING id")["id"]


@pytest.fixture
def key_for(org_id):
    def make(*products: str, rate_per_minute: int = 60, monthly_pages: int | None = None) -> dict:
        return {"Authorization": f"Bearer {create_key(org_id, list(products), rate_per_minute, monthly_pages)}"}
    return make
