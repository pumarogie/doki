import os

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/docforge_test")

import psycopg
import pytest
from fastapi.testclient import TestClient

from docforge import db
from docforge.auth import create_key
from docforge.main import app


@pytest.fixture(scope="session", autouse=True)
def fresh_schema():
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    yield


@pytest.fixture(scope="session")
def client(fresh_schema):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def org_id(client):
    with db.connect() as conn:
        return conn.execute("INSERT INTO organization (name) VALUES ('test') RETURNING id").fetchone()["id"]


@pytest.fixture
def key_for(org_id):
    def make(*products: str) -> dict:
        return {"Authorization": f"Bearer {create_key(org_id, list(products))}"}
    return make
