# syntax=docker/dockerfile:1
# python3.12-trixie-slim (uv 0.13.8, Python 3.12.16), digest re-resolved 2026-11-14 (previous
# pin 17af15d1, built 2026-10-09, carried 3 fixable HIGH OS CVEs: apt CVE-2026-109518 and
# zlib CVE-2026-76844 / CVE-2026-85091). The 2026-11-12 upstream rebuild picked up the apt fix;
# the two zlib CVEs are still unfixed in the image and are scoped-ignored with a reason and an
# expiry in .trivyignore.yaml.
# Earlier history: digest re-resolved 2026-10-10 (previous pin 9a59bb72, resolved
# 2026-09-18, carried 4 HIGH OS CVEs - pcre2 CVE-2026-103111, openssl CVE-2026-84782 - that
# Debian had fixed in deb13u3; the upstream rebuild picked them up). Switched from
# bookworm-slim on 2026-09-18 (see
# DECISIONS.md): astral-sh's own current docs no longer list bookworm-slim in their
# published image catalog (deprecated in favor of trixie-slim), and a Trivy scan of the
# bookworm-slim digest found 7 real CVEs in openssl/libssl3/pcre2 that Debian had already
# patched in bookworm-security - astral-sh's image simply hadn't rebuilt to pick them up,
# with no scheduled rebuild cadence to rely on (their images only rebuild as a side effect
# of a new uv release, not the OS package's own patch cycle).
FROM ghcr.io/astral-sh/uv:python3.12-trixie-slim@sha256:ee8a8854267a3bd09ac0364420803fb74a069068b5fb279a2cf1df664f7273db

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
