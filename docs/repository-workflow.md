# Repository Workflow

Status: active for repository collaboration

This document defines repository-level collaboration rules.
It applies to documentation, research, scaffolding, and implementation work.

## Repository Scope

The repository root is the `mena` product suite and knowledge base.
It is not the Python package root.

Root-level files should stay limited to repository coordination and platform-required files, for example:

- `AGENTS.md`
- `README.md`
- `.gitignore`
- `.github/`
- `docs/`

The runnable software lives under `app/` once scaffolded.
Python packaging, dependency, formatter, linter, test, and local-check configuration should live under `app/` unless a tool explicitly requires root-level configuration.

## Branching Model

Use a lightweight Git Flow variant with `develop` as the active integration branch.

Default branch:

- `main`

Role:

- preserved until the first shippable version
- not used as the normal target for current documentation, research, scaffolding, or implementation work
- updated only when the user explicitly decides to promote a shippable baseline or gives a direct instruction

Integration branch:

- `develop`

Role:

- current active product-development line
- normal target for documentation, research, scaffolding, and implementation changes
- may receive direct commits while the repository is still in early architecture/scaffolding mode and no issue exists yet

Issue work branches:

- branch from `develop`
- open a pull request back to `develop`
- keep branches scoped to one issue or tightly related issue slice
- prefer small PRs that map to one implementation or documentation issue

## Branch Naming

Branch names should include the GitHub issue number and a short slug.

Pattern:

```text
<type>/<issue-number>-<short-kebab-slug>
```

Common branch types:

- `feature/`
- `fix/`
- `docs/`
- `chore/`

Examples:

```text
feature/12-capture-worker-start-stop
docs/9-architecture-guardrails
chore/3-python-project-layout
fix/18-session-lock-recovery
```

If no issue exists yet, create or request an issue before starting substantial work.
Tiny documentation corrections or early architecture/scaffolding updates may be committed directly to `develop` when the user explicitly asks to sync `develop`.

## Commit and Pull Request References

Commits and pull requests should reference the issue number.

Preferred PR body references:

```text
Closes #12
```

or, when the PR is partial:

```text
Refs #12
```

Commit messages should stay concise and may include the issue number when useful:

```text
Implement capture worker start command (#12)
```

## Issue Discipline

Implementation issues should use the GitHub implementation template.
Research tasks are tracked in `docs/research/backlog/` unless explicitly promoted into implementation or documentation work.

Issues should cite:

- requirement references
- architecture references
- implementation guardrail references
- research inputs, if any
- explicit in-scope and out-of-scope work
- verification expectations

Issues are the executable work contract for coding agents.
The architecture remains the reference manual; issues name the specific slice to implement.
