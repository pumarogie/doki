# docforge

One document engine, several PDF products behind one API key system:

| Product | Routes |
|---|---|
| `pdfa` | `POST /v1/pdfa/convert` (PDF/A-2b/3b), `POST /v1/pdfa/validate` (veraPDF) |
| `einvoice` | `POST /v1/einvoice/validate` (XRechnung/EN16931 via KoSIT; XML or ZUGFeRD PDF), `POST /v1/einvoice/zugferd` |
| `render` | `POST /v1/render` (Typst or HTML + JSON data), `/v1/templates` CRUD |
| `extract` | `POST /v1/extract` → job; fields with `value`, `confidence`, `source_page`, `uncertain_fields` |
| `reports` | `/v1/schedules` CRUD, `POST /v1/schedules/{id}/run`; cron + timezone, email or pinned-key SFTP delivery |

Shared: `GET /v1/jobs/{id}`, `GET /v1/files/{id}`, `GET /v1/usage`. Keys are scoped per product.

## Run with Docker

```sh
cp .env.example .env   # fill POSTGRES_PASSWORD, DOCFORGE_OWNER_PASSWORD, DOCFORGE_APP_PASSWORD, DOCFORGE_SECRET_KEY
docker compose up -d --build
docker compose exec api docforge create-key --org acme --products pdfa,einvoice,render,extract,reports
curl -H "Authorization: Bearer <key>" -F file=@doc.pdf localhost:8000/v1/pdfa/validate
```

API docs: http://localhost:8000/docs · Mail catcher: http://localhost:8025

Set `LLM_PROVIDER=claude` and `ANTHROPIC_API_KEY` for real extraction (default model `claude-sonnet-5`,
verification pass `claude-haiku-4-5`; override with `EXTRACT_MODEL` / `VERIFY_MODEL`).

## Develop locally

Needs Python 3.13, uv, Postgres, Ghostscript, veraPDF, Java (KoSIT) and Docker (SFTP tests).

```sh
uv sync
./scripts/fetch-kosit.sh
uv run playwright install chromium
uv run docforge migrate --url postgresql://docforge_owner@localhost/docforge
uv run pytest
uv run uvicorn docforge.main:app --reload    # API
uv run python -m docforge.worker             # jobs + schedules
```

Database roles: migrations run as `docforge_owner`; the app connects as `docforge_app` (DML only).
See `docker/initdb.sh` for the grants.

## Adding a product

Write `src/docforge/products/<name>.py` with an `APIRouter` guarded by `require_product("<name>")`,
add the name to `auth.PRODUCTS` and the `api_key_product` check constraint (new migration),
and `include_router` it in `main.py`. Background work: register a handler with `@jobs.handler(...)`.
