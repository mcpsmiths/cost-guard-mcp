# syntax=docker/dockerfile:1
# python3.12-trixie-slim, digest re-resolved 2026-10-10 (previous pin 9a59bb72, resolved
# 2026-09-18, carried 4 HIGH OS CVEs - pcre2 CVE-2026-103111, openssl CVE-2026-84782 - that
# Debian had fixed in deb13u3; the upstream rebuild picked them up). Switched from
# bookworm-slim on 2026-09-18 (see
# DECISIONS.md): astral-sh's own current docs no longer list bookworm-slim in their
# published image catalog (deprecated in favor of trixie-slim), and a Trivy scan of the
# bookworm-slim digest found 7 real CVEs in openssl/libssl3/pcre2 that Debian had already
# patched in bookworm-security - astral-sh's image simply hadn't rebuilt to pick them up,
# with no scheduled rebuild cadence to rely on (their images only rebuild as a side effect
# of a new uv release, not the OS package's own patch cycle).
FROM ghcr.io/astral-sh/uv:python3.12-trixie-slim@sha256:17af15d103f32869a63825517f9e8867ce98598937e3e5f5ce1e24313c552406

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
