import hashlib
import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from docforge.db import connect

PRODUCTS = ("pdfa", "einvoice", "render", "extract", "reports")

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    api_key_id: int
    organization_id: int
    products: frozenset[str]


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_key(organization_id: int, products: list[str]) -> str:
    """Create an API key and return the plaintext once; only its hash is stored."""
    unknown = set(products) - set(PRODUCTS)
    if unknown:
        raise ValueError(f"unknown products: {sorted(unknown)}")
    prefix = secrets.token_hex(4)
    key = f"df_{prefix}_{secrets.token_urlsafe(32)}"
    with connect() as conn:
        key_id = conn.execute(
            "INSERT INTO api_key (organization_id, prefix, key_hash) VALUES (%s, %s, %s) RETURNING id",
            (organization_id, prefix, hash_key(key)),
        ).fetchone()["id"]
        for p in products:
            conn.execute("INSERT INTO api_key_product (api_key_id, product) VALUES (%s, %s)", (key_id, p))
    return key


def _principal(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing API key")
    with connect() as conn:
        row = conn.execute(
            """SELECT k.id, k.organization_id,
                      coalesce(array_agg(p.product) FILTER (WHERE p.product IS NOT NULL), '{}') AS products
               FROM api_key k LEFT JOIN api_key_product p ON p.api_key_id = k.id
               WHERE k.key_hash = %s AND k.revoked_at IS NULL
               GROUP BY k.id""",
            (hash_key(creds.credentials),),
        ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")
    return Principal(row["id"], row["organization_id"], frozenset(row["products"]))


def require_product(product: str):
    assert product in PRODUCTS, product

    def dep(principal: Principal = Depends(_principal)) -> Principal:
        if product not in principal.products:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "API key not enabled for this product")
        return principal

    return dep


any_key = _principal


def record_usage(principal: Principal, product: str, operation: str, page_count: int = 0) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO usage_event (api_key_id, product, operation, page_count) VALUES (%s, %s, %s, %s)",
            (principal.api_key_id, product, operation, page_count),
        )
