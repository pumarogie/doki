# doki — agent rules

One core document engine (`src/doki/core/`, no HTTP) + one thin router per product
(`src/doki/products/`). Postgres is state + job queue. See README for layout and run steps.

## Done = all four green (run before claiming done or committing)

```sh
uv run ruff check src tests
uv run pyright src tests          # must be 0 errors — Cursor shows the same diagnostics
uv run pytest -q                  # needs local Postgres, gs, verapdf, java+KoSIT, docker (SFTP)
docker compose up -d --build      # only when Dockerfile/compose/deps changed; then curl the touched routes
```

CI (`.github/workflows/ci.yml`) runs the same checks; the pytest job uses the Dockerfile `test` stage.
Keep it green: anything tests need at runtime (binaries like `pkill`) goes into that stage, never into `runtime`.
Compose must keep `target: runtime`.

## Database (psycopg 3)

- Rows are dicts via the typed pool in `db.py`. Never build another pool or call `psycopg.connect` untyped.
- `fetchone()` returns `Optional`. Never write `.fetchone()["x"]`. Use `db.one(conn, sql, params)` when a row
  must exist (INSERT … RETURNING, lookups by FK); otherwise check `is None` and raise `HTTPException(404)`.
- Query arguments must be `LiteralString`, `bytes` or `psycopg.sql`. No f-strings with user data — only constant
  fragments (e.g. column lists). File contents → `path.read_bytes()`.
- `jsonb` does not keep object key order. Store ordered data as lists (e.g. `[[name, desc], ...]`).
- Schema change = new `migrations/NNN_*.sql`, never edit an applied one. Migrations run as `doki_owner`
  (`doki migrate --url …`); the app connects as `doki_app` (DML only) — never grant it DDL.
- 3NF: no column reachable through another FK (e.g. no `organization_id` next to `api_key_id`). Singular table names.
- Every FK gets an index (Postgres doesn't add one); every query path gets one, checked with `EXPLAIN`.
- Tenant isolation: every read/update/delete filters by the caller's organization (directly or via `api_key`).
  Add a cross-org 404 test for each new resource.

## FastAPI

- Guards live at module level: `guard = require_product("x")`, then `Depends(guard)`. Never call a factory in a
  default argument (ruff B008).
- Client-facing errors are generic. Never put tool stderr, stack traces, file paths, hostnames, model names or
  prompt text in a response or `job.error`. Log details; return a message the customer can act on.
- Every authenticated route goes through `auth._principal` (rate limit) and bills via `record_usage`
  (sync) or `charge` (jobs; `QuotaExceeded` → `PermanentError`). Queued work calls `check_quota` before enqueueing.
- Validate at the boundary: size caps, magic bytes (`uploads.read_pdf` also checks the PDF parses), pydantic
  constraints. Heavy/slow work → `jobs.enqueue` + `@jobs.handler`, not in the request.

## External tools & untrusted input

- Exit code 0 does not mean success (Ghostscript turns garbage into an empty PDF). Validate outputs and inputs.
- Run tools via `core/tools.run` (arg list, timeout). Customer code (Typst/HTML templates) runs sandboxed:
  own process + timeout, empty root, no JS, no network.
- Network targets from customers: block private/loopback (`deliver.check_public_host`), pin SSH host keys and
  compare them before sending credentials. Library "generic" errors (e.g. paramiko `SSHException`) can hide
  permanent failures — classify explicitly: `DeliveryError`/`PermanentError` = no retry, everything else retries.
- Customer secrets are Fernet-encrypted (`core/secrets.py`) and never returned by the API.

## Python typing & lint gotchas seen here

- `os.environ.get(name, default)`: default must be a `str` (`"25"`, not `25`).
- `subprocess.run` always gets explicit `check=`.
- Narrow `Optional` returns from libraries (`SFTPClient.from_transport`, `msg.get(...)`) before use.
- Variables assigned only inside a loop must be initialised before it if used after.
- Use `re.IGNORECASE` not `re.I`.

## LLM (`core/llm.py` is the only module that calls a model)

- Default `LLM_PROVIDER=mock`; tests always run mock. A real call costs money — ask before running one.
- Structured output via `output_config.format` json_schema; handle `refusal`/`max_tokens` stop reasons;
  4xx → `LLMError` (permanent), 429/5xx/network → propagate (retried).
- Never log document text or model output.

## Process

- Commits: no AI attribution lines (global rule). One behavior each, ≤5 files, explicit paths, `feat`/`fix`/`refactor`/`test`/`docs`/`chore` separate.
- A bug gets a failing test first, then the fix.
- The user may be editing in Cursor at the same time: re-read a file before editing it, keep their changes.
- Comments: plain prose, no tag prefixes (the user removed `ponytail:` tags here).
- IP: no code, prompts, schemas, fixtures or data from other employers or projects. Public docs only.
