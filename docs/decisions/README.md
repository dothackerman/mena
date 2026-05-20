# Decision Records

This directory contains all decision records for the mena project. Decisions are immutable once accepted — they are superseded, not edited.

## Types

| Prefix | Owner | Focus |
|--------|-------|-------|
| **ADR** | Architect | Structural decisions — component boundaries, interfaces, technology choices, integration patterns |
| **PDR** | PM | Product decisions — user-facing behavior, scope constraints, experience rules |

Both types can reference each other for traceability. References are optional, not mandatory.

## Status Lifecycle

```
Proposed → Accepted → [Superseded | Deprecated | Rejected]
```

- **Proposed** — drafted, not yet agreed
- **Accepted** — agreed and active
- **Superseded** — replaced by a new decision. Must reference the successor.
- **Deprecated** — no longer relevant. Must include reason.
- **Rejected** — never accepted. Kept for the record.

**Decisions are never deleted.**

## Template

```markdown
# [ADR|PDR]-NNN: [title]

- **Type:** Architecture | Product
- **Status:** Proposed | Accepted | Superseded | Deprecated | Rejected
- **Date:** YYYY-MM-DD
- **Superseded by:** [ADR-NNN | PDR-NNN | —]
- **Related:** #issue, PR #nn, ADR-NNN, PDR-NNN

## Context

[Why we're making this decision. What forces are at play?]

## Decision

[What we decided. Be specific and unambiguous.]

## Consequences

[What this means — positive and negative. What becomes easier or harder?]
```

## Index

### Existing decisions (pre-PDR/ADR format)

Decisions AD-001 through AD-027 were recorded in the architecture overview before this directory existed. They remain the canonical record for those decisions and are **not** extracted into separate files.

See: [`docs/architecture/architecture-overview.md §9.1 Decision Log`](../architecture/architecture-overview.md)

### New decisions (file-per-decision)

| # | Type | Title | Status | Date |
|---|------|-------|--------|------|
| PDR-001 | Product | Automatic correction overlay selection | Accepted | 2026-05-20 |
