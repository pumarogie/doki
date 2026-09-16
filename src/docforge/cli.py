import argparse
import os

from docforge import db
from docforge.auth import PRODUCTS, create_key


def main() -> None:
    parser = argparse.ArgumentParser(prog="docforge")
    sub = parser.add_subparsers(dest="cmd", required=True)
    mig = sub.add_parser("migrate", help="apply migrations as the schema owner")
    mig.add_argument("--url", default=os.environ.get("MIGRATE_DATABASE_URL"),
                     help="owner connection string (default: $MIGRATE_DATABASE_URL)")
    ck = sub.add_parser("create-key", help="create an organization (if needed) and an API key")
    ck.add_argument("--org", required=True, help="organization name")
    ck.add_argument("--products", default=",".join(PRODUCTS))
    args = parser.parse_args()

    if args.cmd == "migrate":
        if not args.url:
            parser.error("set --url or MIGRATE_DATABASE_URL")
        print("applied:", db.migrate(args.url) or "nothing")
        return

    db.pool.open()
    try:
        with db.connect() as conn:
            row = conn.execute("SELECT id FROM organization WHERE name = %s", (args.org,)).fetchone()
            org_id = row["id"] if row else conn.execute(
                "INSERT INTO organization (name) VALUES (%s) RETURNING id", (args.org,)
            ).fetchone()["id"]
        print(create_key(org_id, args.products.split(",")))
    finally:
        db.pool.close()
