# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

RUN uv sync --frozen --no-dev

# Invoke the already-synced venv's own entry point directly, not `uv run` - `uv run`
# re-checks/re-syncs the project against pyproject.toml/uv.lock on every invocation, which
# adds startup latency and writes "Building.../Installed..." noise on every container start
# for no benefit here (the image is immutable once built).
#
# stdio transport: talks over stdin/stdout, so no port to expose. Credentials come in via
# env vars at `docker run -e ...` time (see README "Setup") - never baked into the image.
ENTRYPOINT ["/app/.venv/bin/cost-guard-mcp"]
