import argparse

from docforge import db
from docforge.auth import PRODUCTS, create_key


def main() -> None:
    parser = argparse.ArgumentParser(prog="docforge")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    ck = sub.add_parser("create-key", help="create an organization (if needed) and an API key")
    ck.add_argument("--org", required=True, help="organization name")
    ck.add_argument("--products", default=",".join(PRODUCTS))
    args = parser.parse_args()

    db.pool.open()
    try:
        if args.cmd == "migrate":
            print("applied:", db.migrate() or "nothing")
        elif args.cmd == "create-key":
            db.migrate()
            with db.connect() as conn:
                row = conn.execute("SELECT id FROM organization WHERE name = %s", (args.org,)).fetchone()
                org_id = row["id"] if row else conn.execute(
                    "INSERT INTO organization (name) VALUES (%s) RETURNING id", (args.org,)
                ).fetchone()["id"]
            print(create_key(org_id, args.products.split(",")))
    finally:
        db.pool.close()
