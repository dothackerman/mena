# AGENTS.md

Repository-local guidance for agents working in `mena`.

## Repository Scope

The repository root represents the whole `mena` product suite and knowledge base, not only the Python software package.

Repository-root concerns include:

- requirements
- architecture
- research
- deferred ideas
- GitHub workflow metadata
- future product and operational documentation

The installable/runnable software lives under `app/` once scaffolded.
Do not place Python package tooling or application-specific configuration in the repository root unless the tool requires a root-level location.

## Folder Semantics

### `docs/requirements/`

Purpose:

- source inputs from prior discussions
- requirements and discovery artifacts that architecture depends on

Default handling:

- treat as read-only inputs
- do not silently reinterpret or rewrite
- only edit when explicitly asked to revise the source artifact itself

Scope evolution:

- `docs/requirements/poc-requirements.md` is a v1.0 snapshot dated 2026-04-14 and remains read-only by design
- subsequent scope decisions live in `docs/architecture/architecture-overview.md` §9.1 Decision Log
- deferred items live in `docs/ideas/`, each with a "Where It Comes From" back-reference to the originating requirement
- before treating any requirement as authoritative, check the AD log and `docs/ideas/` for supersedences

### `docs/architecture/`

Purpose:

- evolving architectural synthesis
- decisions, open questions, tradeoffs, diagrams, and later ADR references

Default handling:

- this is a working area and may be updated during architecture discussions
- keep decision status explicit when useful, for example `Confirmed`, `Provisional`, `Deferred`
- keep architecture narrative separate from raw research output

### `docs/research/notes/`

Purpose:

- bounded or deep research outputs
- evidence artifacts that inform architecture decisions

Default handling:

- treat as research outputs, not scratchpads
- prefer adding a new note or clearly marked revision over silently rewriting conclusions
- only make small factual or formatting corrections in place unless asked to revisit the research

### `docs/research/backlog/`

Purpose:

- pending research tasks derived from architecture work
- questions that need investigation before hard decisions or implementation

Default handling:

- store actionable research questions here
- distinguish backlog items from completed research notes
- do not mix implementation task lists into this folder

### `docs/ideas/`

Purpose:

- deferred product or architecture ideas that surfaced during discovery, architecture, review, or implementation
- provenance records for ideas that are explicitly outside current PoC implementation scope

Default handling:

- do not read this folder for normal coding tasks unless a user request or issue explicitly references a specific idea
- do not expand implementation scope based on ideas in this folder
- keep each idea in its own markdown file with a clear "where it comes from" section
- ideas are not requirements, research results, or implementation tasks

## Decision vs Research Distinction

Use `docs/architecture/` for:

- current decisions
- open architectural questions
- tradeoff reasoning
- implementation guardrails that explain how coding agents should interpret architecture during PoC work

Use `docs/research/backlog/` for:

- questions that need evidence, comparison, or investigation
- unresolved topics whose answers affect architecture or implementation strategy

Use `docs/research/notes/` for:

- completed research outputs

Use `docs/ideas/` for:

- deferred ideas and provenance only
- non-binding future direction that should not distract coding agents

## Working Style

- Prefer folder-level rules over file-specific exceptions.
- Preserve the difference between source inputs, architecture synthesis, research backlog, and research outputs.
- Keep repository structure legible for future coding and research agents.
- Follow the repository workflow in `docs/repository-workflow.md` for branch naming and issue references.
- For GitHub issue relationships, use native GitHub metadata. Do not encode parent/child, blocking, or related-issue lists in issue bodies when GitHub can represent the relationship directly.
- Treat `develop` as the active integration branch. Do not target `main` until the first shippable version or unless the user explicitly instructs it.

## Developer vs Product Usage

- Keep repository/developer usage distinct from installed product or UAT usage, even on the same machine.
- Run unreleased code from the checkout with repo-scoped commands such as `cd app && uv run mena ...`.
- Treat plain `mena ...` as the installed product/UAT command unless the current shell environment explicitly points at the repo virtual environment.
- Dev smoke tests, fixtures, and agent validation should use explicit scratch paths such as `MENA_DATA_ROOT`, temporary XDG roots, or test fixtures. Do not rely on default product data paths for repo validation unless the user explicitly asks to test the installed product setup.
- Installed product/UAT usage should use normal runtime paths and must not depend on the repo checkout, editable package state, or test fixtures.
- When changing install, runtime path, or preflight behavior, verify which `mena` executable is being exercised and state whether validation used repo/dev invocation or installed product invocation.

## Commit Hygiene

- Commit iteratively while working. Do not save a whole feature run as one large final commit.
- Group changes semantically: schema/contract changes, implementation behavior, tests, documentation, tooling, and review fixes should be separate commits unless they are tightly coupled.
- Keep commits small enough to review. There is no arbitrary file-count limit, but a commit with thousands of changed lines across multiple concerns is not acceptable.
- A useful implementation issue usually has at least these possible commit slices when they are independently reviewable: contract/schema, runtime behavior, tests, and docs. Combine slices only when separating them would make the intermediate commits misleading or broken.
- If you realize after committing that the branch shape violates this guidance, do not pretend it is fine. Preserve the visible history unless the user asks for a rewrite, then add a follow-up note or commit that calls out the process miss.
- Preserve learning history. If another agent produced a poor commit shape, do not rewrite that history unless the user explicitly asks for a history rewrite. Add follow-up commits and document the issue so the team can learn from the failure.

## Definition of Done for Issue Work

Issue work is not done when the code compiles. It is done when the branch is ready for another reviewer.

- Open a draft pull request once there is a coherent working slice worth tracking in GitHub.
- Keep the pull request as a draft while implementation, local validation, and self-review are still in progress.
- Before marking the pull request ready for review, perform a local self-review of the diff against the issue scope, linked architecture guidance, non-goals, and likely edge cases.
- Add self-review fixes as follow-up commits. Do not silently rewrite another agent's history to hide review findings unless the user explicitly asks for that.
- Run the relevant local gates, including `make check` from `app/` when application code changes.
- Update the pull request body with scope, validation results, known limitations or non-goals, useful agent-run notes, and a short local self-review section that states what was checked and what was fixed or left for reviewers.
- Only after the self-review is complete, fixes are pushed, gates pass, and the PR body is current, mark the pull request ready for review.
