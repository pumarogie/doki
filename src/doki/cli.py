import argparse
import json
import os

from doki import db
from doki.auth import PRODUCTS, create_key, revoke_key


def main() -> None:
    parser = argparse.ArgumentParser(prog="doki")
    sub = parser.add_subparsers(dest="cmd", required=True)
    mig = sub.add_parser("migrate", help="apply migrations as the schema owner")
    mig.add_argument("--url", default=os.environ.get("MIGRATE_DATABASE_URL"),
                     help="owner connection string (default: $MIGRATE_DATABASE_URL)")
    ck = sub.add_parser("create-key", help="create an organization (if needed) and an API key")
    ck.add_argument("--org", required=True, help="organization name")
    ck.add_argument("--products", default=",".join(PRODUCTS))
    ck.add_argument("--rate-per-minute", type=int, default=60)
    ck.add_argument("--monthly-pages", type=int, default=None, help="page quota per calendar month (default: unlimited)")
    rk = sub.add_parser("revoke-key", help="revoke an API key by its visible prefix (dk_<prefix>_...)")
    rk.add_argument("--prefix", required=True)
    sub.add_parser("ensure-bucket", help="create the S3 bucket if STORAGE_BACKEND=s3 (no-op for local)")
    tr = sub.add_parser("trace", help="find the API key behind a leaked PDF (reads its trace id)")
    tr.add_argument("pdf", help="path to the PDF")
    ev = sub.add_parser("eval-extract", help="measure extraction accuracy on synthetic invoices")
    ev.add_argument("--cases", type=int, default=50)
    ev.add_argument("--seed", type=int, default=7)
    ev.add_argument("--min-accuracy", type=float, default=None, help="exit 1 if accuracy is below this")
    args = parser.parse_args()

    if args.cmd == "eval-extract":
        from doki import evaluation

        report = evaluation.run(args.cases, args.seed)
        print(json.dumps(report, indent=2))
        if args.min_accuracy is not None and report["accuracy"] < args.min_accuracy:
            raise SystemExit(f"accuracy {report['accuracy']} < {args.min_accuracy}")
        return

    if args.cmd == "ensure-bucket":
        from doki.core import blobs

        store = blobs.store
        if isinstance(store, blobs.S3Blobs):
            existing = {b["Name"] for b in store.client.list_buckets().get("Buckets", [])}
            if store.bucket not in existing:
                store.client.create_bucket(Bucket=store.bucket)
            print(f"bucket ready: {store.bucket}")
        else:
            print("local storage, nothing to do")
        return

    if args.cmd == "migrate":
        if not args.url:
            parser.error("set --url or MIGRATE_DATABASE_URL")
        print("applied:", db.migrate(args.url) or "nothing")
        return

    db.pool.open()
    try:
        if args.cmd == "trace":
            from pathlib import Path

            from doki.auth import trace
            from doki.core import watermark

            trace_id = watermark.read(Path(args.pdf).read_bytes())
            print(json.dumps({"trace_id": trace_id, "usage": trace(trace_id) if trace_id else None}, default=str, indent=2))
            return
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
