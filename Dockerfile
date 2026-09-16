# veraPDF ships as platform-independent jars plus a shell launcher; copy them from the official image.
FROM --platform=linux/amd64 verapdf/cli:latest AS verapdf

FROM python:3.13-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    KOSIT_DIR=/opt/kosit \
    STORAGE_DIR=/data/files \
    PATH=/app/.venv/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends ghostscript openjdk-17-jre-headless curl unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
COPY --from=verapdf /opt/verapdf /opt/verapdf
RUN ln -s /opt/verapdf/verapdf /usr/local/bin/verapdf

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
RUN playwright install --with-deps --only-shell chromium
COPY scripts ./scripts
RUN ./scripts/fetch-kosit.sh "$KOSIT_DIR"

COPY migrations ./migrations
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 app && mkdir -p /data/files && chown app /data/files
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fsS http://localhost:8000/health || exit 1
CMD ["uvicorn", "docforge.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
