# syntax=docker/dockerfile:1
# Multi-stage build: dependencies are resolved with uv in a builder stage, and only the
# virtualenv + source are copied into a slim runtime image that runs as a non-root user.

ARG PYTHON_IMAGE=python:3.12.11-slim-bookworm

FROM ${PYTHON_IMAGE} AS builder
COPY --from=ghcr.io/astral-sh/uv:0.8.15 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first (cached layer), project code second: code edits don't reinstall deps.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM ${PYTHON_IMAGE}
# Apply Debian security updates at build time: a pinned base tag is frozen, so without this
# fixed OS CVEs (e.g. CVE-2026-57433 in perl-base) stay in the image and fail the Trivy gate.
# pip is removed because nothing installs packages at runtime (smaller attack surface).
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip uninstall -y pip
# Fixed numeric UID so Kubernetes `runAsNonRoot` can verify it without a passwd lookup.
RUN groupadd --system --gid 10001 gateway \
 && useradd --system --uid 10001 --gid gateway --no-create-home gateway
WORKDIR /app
COPY --from=builder --chown=root:root /app/.venv /app/.venv
COPY --chown=root:root src ./src
COPY --chown=root:root alembic.ini ./
COPY --chown=root:root migrations ./migrations
# Code is owned by root and only readable by the app user: a compromised process can't
# rewrite itself. PYTHONUNBUFFERED gets logs out immediately.
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/healthz').status != 200)"
# --no-access-log: the app writes its own structured access log.
CMD ["uvicorn", "--factory", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--proxy-headers"]
