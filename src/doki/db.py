from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from psycopg import Connection
from psycopg.abc import Params, QueryNoTemplate
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from doki.config import settings

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"

pool: ConnectionPool[Connection[DictRow]] = ConnectionPool(
    settings.database_url,
    open=False,
    connection_class=Connection[DictRow],
    kwargs={"row_factory": dict_row},
)


@contextmanager
def connect() -> Iterator[Connection[DictRow]]:
    with pool.connection() as conn:
        yield conn


def one(conn: Connection[DictRow], query: QueryNoTemplate, params: Params | None = None) -> DictRow:
    """Fetch exactly one row; for queries that must return one (e.g. INSERT ... RETURNING)."""
    row = conn.execute(query, params).fetchone()
    if row is None:
        raise LookupError("query returned no row")
    return row


def migrate(conninfo: str) -> list[str]:
    """Apply pending migrations/*.sql in name order, each in its own transaction.

    Runs as the schema owner role (DDL); the app itself connects as a DML-only role.
    """
    applied = []
    with Connection[DictRow].connect(conninfo, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migration (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {r["name"] for r in conn.execute("SELECT name FROM schema_migration")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():
                conn.execute(path.read_bytes())  # file content, not user input
                conn.execute("INSERT INTO schema_migration (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
    return applied
