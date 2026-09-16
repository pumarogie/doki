# veraPDF ships as platform-independent jars plus a shell launcher; copy them from the official image.
# 1.30.2
FROM --platform=linux/amd64 verapdf/cli@sha256:d5ee329657cf9bc4b2400392dd54c7d0a0ce9980ff6fa2da5590eebeec007cdb AS verapdf

FROM python:3.14-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    KOSIT_DIR=/opt/kosit \
    STORAGE_DIR=/data/files \
    PATH=/app/.venv/bin:$PATH

RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends ghostscript openjdk-17-jre-headless curl unzip ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
# The app runs from uv's venv; the base image's pip (and the libraries it vendors) is never used.
RUN python -m pip uninstall -y pip
COPY --from=verapdf /opt/verapdf /opt/verapdf
RUN ln -s /opt/verapdf/verapdf /usr/local/bin/verapdf

WORKDIR /app
COPY pyproject.toml uv.lock ./
# The uv cache stays in a build cache mount, not in the image.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
RUN playwright install --with-deps --only-shell chromium
COPY scripts ./scripts
RUN ./scripts/fetch-kosit.sh "$KOSIT_DIR"

COPY migrations ./migrations
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

RUN useradd --system --uid 10001 app && mkdir -p /data/files && chown app /data/files
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fsS http://localhost:8000/health || exit 1
CMD ["uvicorn", "doki.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-access-log"]

# CI image: runtime + dev dependencies + tests. Build with --target test.
FROM runtime AS test
USER root
RUN apt-get update && apt-get install -y --no-install-recommends procps && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen
COPY tests ./tests
RUN chown -R app /app
USER app
CMD ["pytest", "-q"]
