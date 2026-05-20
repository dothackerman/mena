# ADR-019: Centralize error recovery authority in the active orchestrator

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-04-20
- **Superseded by:** —
- **Related:** ADR-004, ADR-006

## Context

If recovery authority is distributed, adapters may make conflicting decisions about how to handle failures.

## Decision

Centralize error recovery authority in the active orchestrator.

## Consequences

Adapters report typed failures; the orchestrator chooses bounded recovery according to stage policy, artifact state, and user intent.
