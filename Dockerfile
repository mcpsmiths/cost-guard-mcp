# syntax=docker/dockerfile:1
# python3.12-trixie-slim (uv 0.13.10, Python 3.12.16, Debian 13.8), digest re-resolved
# 2026-11-26 (previous pin ee8a8854, built 2026-11-12 with uv 0.13.8, carried two fixable HIGH
# zlib CVEs, CVE-2026-76844 and CVE-2026-85091, which the 2026-11-24 upstream rebuild fixes).
# One fixable HIGH OS CVE is still in the image: libzstd1 CVE-2026-124851 (published
# 2026-11-24, Debian fix 1.5.7+dfsg-1+deb13u1 not yet in the upstream image). It is
# scoped-ignored with a reason and an expiry in .trivyignore.yaml.
# Earlier history: digest re-resolved 2026-11-14 (previous pin 17af15d1, built 2026-10-09,
# carried apt CVE-2026-109518 and the two zlib CVEs; the 2026-11-12 rebuild picked up the apt fix).
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
FROM ghcr.io/astral-sh/uv:python3.12-trixie-slim@sha256:a3fdff607889036fef414975e3701d14a2b6734ffec10cbdd5ce60d2b6c8853a

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
