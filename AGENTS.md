# AGENTS.md

Repository-local guidance for agents working in `mena`.

## Folder Semantics

### `docs/requirements/`

Purpose:

- source inputs from prior discussions
- requirements and discovery artifacts that architecture depends on

Default handling:

- treat as read-only inputs
- do not silently reinterpret or rewrite
- only edit when explicitly asked to revise the source artifact itself

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
