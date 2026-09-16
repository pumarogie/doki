import hashlib
import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from docforge import logs
from docforge.db import connect, one

PRODUCTS = ("pdfa", "einvoice", "render", "extract", "reports")

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    api_key_id: int
    organization_id: int
    products: frozenset[str]
    rate_per_minute: int
    monthly_pages: int | None


class QuotaExceeded(Exception):
    """The key's monthly page quota is used up."""


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_key(organization_id: int, products: list[str], rate_per_minute: int = 60, monthly_pages: int | None = None) -> str:
    """Create an API key and return the plaintext once; only its hash is stored."""
    unknown = set(products) - set(PRODUCTS)
    if unknown:
        raise ValueError(f"unknown products: {sorted(unknown)}")
    prefix = secrets.token_hex(4)
    key = f"df_{prefix}_{secrets.token_urlsafe(32)}"
    with connect() as conn:
        key_id = one(
            conn,
            """INSERT INTO api_key (organization_id, prefix, key_hash, rate_per_minute, monthly_pages)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (organization_id, prefix, hash_key(key), rate_per_minute, monthly_pages),
        )["id"]
        for p in products:
            conn.execute("INSERT INTO api_key_product (api_key_id, product) VALUES (%s, %s)", (key_id, p))
    return key


def revoke_key(prefix: str) -> bool:
    with connect() as conn:
        return conn.execute(
            "UPDATE api_key SET revoked_at = now() WHERE prefix = %s AND revoked_at IS NULL", (prefix,)
        ).rowcount == 1


def _principal(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing API key")
    with connect() as conn:
        row = conn.execute(
            """SELECT k.id, k.organization_id, k.rate_per_minute, k.monthly_pages,
                      coalesce(array_agg(p.product) FILTER (WHERE p.product IS NOT NULL), '{}') AS products
               FROM api_key k LEFT JOIN api_key_product p ON p.api_key_id = k.id
               WHERE k.key_hash = %s AND k.revoked_at IS NULL
               GROUP BY k.id""",
            (hash_key(creds.credentials),),
        ).fetchone()
        if row is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")
        window = one(
            conn,
            """INSERT INTO rate_window (api_key_id, window_start, hits)
               VALUES (%s, date_trunc('minute', now()), 1)
               ON CONFLICT (api_key_id, window_start) DO UPDATE SET hits = rate_window.hits + 1
               RETURNING hits, extract(epoch FROM window_start + interval '1 minute' - now())::int AS retry_after""",
            (row["id"],),
        )
    if window["hits"] > row["rate_per_minute"]:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "rate limit exceeded",
            headers={"Retry-After": str(max(1, window["retry_after"]))},
        )
    return Principal(row["id"], row["organization_id"], frozenset(row["products"]), row["rate_per_minute"], row["monthly_pages"])


def require_product(product: str):
    assert product in PRODUCTS, product

    def dep(principal: Principal = Depends(_principal)) -> Principal:
        if product not in principal.products:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "API key not enabled for this product")
        return principal

    return dep


any_key = _principal


def pages_used_this_month(conn, api_key_id: int) -> int:
    return one(
        conn,
        """SELECT coalesce(sum(page_count), 0)::int AS pages FROM usage_event
           WHERE api_key_id = %s AND created_at >= date_trunc('month', now())""",
        (api_key_id,),
    )["pages"]


def charge(api_key_id: int, product: str, operation: str, page_count: int = 0) -> None:
    """Record usage, refusing when it would exceed the key's monthly page quota."""
    with connect() as conn, conn.transaction():
        quota = one(conn, "SELECT monthly_pages FROM api_key WHERE id = %s FOR UPDATE", (api_key_id,))["monthly_pages"]
        if quota is not None and pages_used_this_month(conn, api_key_id) + page_count > quota:
            raise QuotaExceeded
        conn.execute(
            """INSERT INTO usage_event (api_key_id, product, operation, page_count, request_id)
               VALUES (%s, %s, %s, %s, %s)""",
            (api_key_id, product, operation, page_count, logs.request_id.get()),
        )


def record_usage(principal: Principal, product: str, operation: str, page_count: int = 0) -> None:
    """Sync-route variant of charge(): the work is already done, so over-quota becomes a 402."""
    try:
        charge(principal.api_key_id, product, operation, page_count)
    except QuotaExceeded:
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "monthly page quota exceeded")


def check_quota(principal: Principal, page_count: int) -> None:
    """Cheap pre-check before enqueuing work; the job charges for real when it runs."""
    if principal.monthly_pages is None:
        return
    with connect() as conn:
        if pages_used_this_month(conn, principal.api_key_id) + page_count > principal.monthly_pages:
            raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "monthly page quota exceeded")


def trace(request_id: str) -> dict | None:
    """Which key produced a document stamped with this trace id."""
    with connect() as conn:
        return conn.execute(
            """SELECT u.request_id, u.product, u.operation, u.created_at, k.prefix, k.organization_id, o.name AS organization
               FROM usage_event u JOIN api_key k ON k.id = u.api_key_id JOIN organization o ON o.id = k.organization_id
               WHERE u.request_id = %s ORDER BY u.created_at LIMIT 1""",
            (request_id,),
        ).fetchone()


def prune_rate_windows() -> int:
    with connect() as conn:
        return conn.execute("DELETE FROM rate_window WHERE window_start < now() - interval '1 hour'").rowcount
