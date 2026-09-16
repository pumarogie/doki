import argparse
import os

from docforge import db
from docforge.auth import PRODUCTS, create_key, revoke_key


def main() -> None:
    parser = argparse.ArgumentParser(prog="docforge")
    sub = parser.add_subparsers(dest="cmd", required=True)
    mig = sub.add_parser("migrate", help="apply migrations as the schema owner")
    mig.add_argument("--url", default=os.environ.get("MIGRATE_DATABASE_URL"),
                     help="owner connection string (default: $MIGRATE_DATABASE_URL)")
    ck = sub.add_parser("create-key", help="create an organization (if needed) and an API key")
    ck.add_argument("--org", required=True, help="organization name")
    ck.add_argument("--products", default=",".join(PRODUCTS))
    ck.add_argument("--rate-per-minute", type=int, default=60)
    ck.add_argument("--monthly-pages", type=int, default=None, help="page quota per calendar month (default: unlimited)")
    rk = sub.add_parser("revoke-key", help="revoke an API key by its visible prefix (df_<prefix>_...)")
    rk.add_argument("--prefix", required=True)
    args = parser.parse_args()

    if args.cmd == "migrate":
        if not args.url:
            parser.error("set --url or MIGRATE_DATABASE_URL")
        print("applied:", db.migrate(args.url) or "nothing")
        return

    db.pool.open()
    try:
        if args.cmd == "revoke-key":
            print("revoked" if revoke_key(args.prefix) else "no active key with that prefix")
            return
        with db.connect() as conn:
            row = conn.execute("SELECT id FROM organization WHERE name = %s", (args.org,)).fetchone()
            if row is None:
                row = db.one(conn, "INSERT INTO organization (name) VALUES (%s) RETURNING id", (args.org,))
            org_id = row["id"]
        print(create_key(org_id, args.products.split(","), args.rate_per_minute, args.monthly_pages))
    finally:
        db.pool.close()
