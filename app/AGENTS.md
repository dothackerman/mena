# AGENTS.md

Guidance for agents working in `app/`.

## Scope

`app/` contains the runnable Python software.
The repository root is the whole `mena` product suite and knowledge base, not the Python package root.

If global or user-level Codex instructions conflict with this file for `app/` work, follow this file unless a higher-priority system, developer, or explicit user instruction says otherwise.

## Tooling

- Use `uv` for Python dependency management and command execution.
- Keep Python package, formatter, linter, test, and type-check configuration in `app/pyproject.toml`.
- Do not add Python application tooling configuration to the repository root unless a tool explicitly requires it.
- Run `make check` from `app/` before handoff when application code changes.

## Package Layout

- Use the `src/` layout.
- Keep importable application code under `app/src/mena/`.
- Keep tests under `app/tests/`.
- Keep committed test fixtures under `app/fixtures/`.
- Do not commit private meeting recordings or real customer meeting content as fixtures.

## Architecture Interpretation

- Follow `docs/architecture/architecture-overview.md`.
- Follow `docs/architecture/implementation-guardrails.md`.
- Preserve architecture boundaries, but implement the smallest useful PoC slice behind each boundary.
- Do not implement deferred ideas from `docs/ideas/` unless an issue or user request explicitly promotes them.

