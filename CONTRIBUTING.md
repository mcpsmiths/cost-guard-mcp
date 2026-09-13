# Contributing

Thanks for considering a contribution. This is a small, single-maintainer project, so
please open an issue before starting substantial work — it saves both of us from a PR
that doesn't fit the project's direction.

## Setup, build, lint, test

See [`AGENTS.md`](AGENTS.md) — it's the single source of truth for the exact commands,
so they're not duplicated here and can't drift out of sync.

## Before opening a PR

- [ ] `uv run ruff check src tests` passes
- [ ] `uv run mypy src` passes
- [ ] `uv run pytest tests/unit -v --cov=cost_guard_mcp --cov-fail-under=80` passes
- [ ] A bug fix includes a regression test in the same commit
- [ ] Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
      (`feat:`, `fix:`, `docs:`, `test:`, `chore:`, `ci:`)

## Pull requests

- Branch off `main`: `feat/...`, `fix/...`, or `docs/...`.
- Keep PRs scoped to one change — easier to review, easier to revert if something's wrong.
- CI must be green before merge.

## Reporting a security issue

Don't open a public issue — see [`SECURITY.md`](SECURITY.md).

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
