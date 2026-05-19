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

## Commit Hygiene

- Commit app changes iteratively in small, semantic slices.
- Separate contracts/schemas, implementation behavior, tests, tooling, and review fixes when they are independently reviewable.
- Do not collapse a full feature into one oversized commit. There is no hard file-count maximum, but thousands of changed lines across several concerns in one commit is a review failure.
- Prefer commit slices reviewers can reason about independently: persisted contract/schema, implementation behavior, tests, documentation, and review fixes. It is acceptable to combine tightly coupled changes when a separated commit would be misleading or non-working.
- Do not rewrite another agent's branch history to hide poor commit shape unless the user explicitly requests it. Prefer additive fix commits and explicit review notes.

## Definition of Done for App Issue Work

- App issue work should end in a pull request, not only local commits.
- Open the pull request as a draft once there is a coherent working slice worth tracking in GitHub.
- Keep it in draft while implementation, validation, and local self-review are still active.
- Before marking ready for review, self-review the app diff for issue completion, architecture boundaries, persisted contract/schema parity, checkpoint/reuse behavior, failure paths, and negative tests.
- Add self-review fixes as follow-up commits unless the user explicitly asks for a history rewrite.
- Run `make check` from `app/` and update the PR body with current validation results plus a short local self-review section that names the review checklist used and any fixes made from it.
- Mark the PR ready for review only after self-review fixes are pushed, gates pass, and the PR description is current.
