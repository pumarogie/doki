# docforge

One document engine, several PDF products behind one API key system:

| Product | Routes |
|---|---|
| `pdfa` | `POST /v1/pdfa/convert` (PDF/A-2b/3b), `POST /v1/pdfa/validate` (veraPDF) |
| `einvoice` | `POST /v1/einvoice/validate` (XRechnung/EN16931 via KoSIT; XML or ZUGFeRD PDF), `POST /v1/einvoice/zugferd` |
| `render` | `POST /v1/render` (Typst or HTML + JSON data), `/v1/templates` CRUD |
| `extract` | `POST /v1/extract` → job; fields with `value`, `confidence`, `source_page`, `uncertain_fields` |
| `reports` | `/v1/schedules` CRUD, `POST /v1/schedules/{id}/run`; cron + timezone, email or pinned-key SFTP delivery |

Shared: `GET /v1/jobs/{id}`, `GET /v1/files/{id}`, `GET /v1/usage`. Keys are scoped per product and carry
limits: requests per minute (429 + `Retry-After`) and an optional monthly page quota (402).

```sh
docforge create-key --org acme --products pdfa,render --rate-per-minute 120 --monthly-pages 5000
docforge revoke-key --prefix <prefix>        # the part after df_ in the key
```

## Webhooks

`POST /v1/webhooks {"url": "https://…"}` returns a `whsec_…` secret once. Every finished job sends
`job.succeeded` / `job.failed` as JSON with `Docforge-Signature: t=<ts>,v1=<hmac>`, where
`hmac = HMAC-SHA256(secret, "<ts>." + raw_body)`. Verify it and reject old timestamps.
5xx/timeouts are retried; 4xx is final; redirects are never followed.

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

## Extraction quality

`docforge eval-extract --cases 50` renders 50 synthetic German/English invoices (varied layouts, date and
amount formats, missing fields) and reports per-field accuracy plus confidence calibration (ECE).
The default mock provider only checks the harness; set `LLM_PROVIDER=claude` for a real (paid) run and
gate with `--min-accuracy 0.95`.

## CI

`.github/workflows/ci.yml` runs ruff + pyright, then builds `docker build --target test` (the runtime image plus
dev dependencies and tests) and runs pytest against a Postgres service prepared by `docker/initdb.sh`
(`DOCFORGE_DB=docforge_test`). Reproduce locally:

```sh
docker build --target test -t docforge:test .
# start postgres:17, run docker/initdb.sh against it, then:
docker run --rm -e DATABASE_URL=postgresql://docforge_app:…@host/docforge_test \
  -e TEST_OWNER_DATABASE_URL=postgresql://docforge_owner:…@host/docforge_test docforge:test
```

## Adding a product

Write `src/docforge/products/<name>.py` with an `APIRouter` guarded by `require_product("<name>")`,
add the name to `auth.PRODUCTS` and the `api_key_product` check constraint (new migration),
and `include_router` it in `main.py`. Background work: register a handler with `@jobs.handler(...)`.
