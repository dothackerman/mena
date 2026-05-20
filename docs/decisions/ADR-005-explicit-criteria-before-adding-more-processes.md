# ADR-005: Explicit criteria before adding more processes

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-04-20
- **Superseded by:** —
- **Related:** ADR-004

## Context

Multi-process architectures add complexity. New processes must justify their existence through one of the documented criteria rather than being introduced casually.

## Decision

Use explicit criteria before adding more processes. New processes must justify themselves through failure isolation, real-time ownership, resource isolation, lifecycle independence, privilege boundary, or runtime incompatibility.

## Consequences

The process topology stays minimal and each process has a clear justification.
