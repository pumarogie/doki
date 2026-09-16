from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from docforge.config import settings

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"

pool = ConnectionPool(settings.database_url, open=False, kwargs={"row_factory": dict_row})


@contextmanager
def connect():
    with pool.connection() as conn:
        yield conn


def migrate(conninfo: str) -> list[str]:
    """Apply pending migrations/*.sql in name order, each in its own transaction.

    Runs as the schema owner role (DDL); the app itself connects as a DML-only role.
    """
    applied = []
    with psycopg.connect(conninfo, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migration (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {r["name"] for r in conn.execute("SELECT name FROM schema_migration")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migration (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
    return applied
