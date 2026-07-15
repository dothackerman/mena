# ADR-018: Narrow rebuildable global index

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-04-20
- **Superseded by:** —
- **Related:** —

## Context

A global index that stores meeting content would become a parallel source of truth, competing with session bundles for canonicity.

## Decision

Use a narrow rebuildable global index.

## Implementation status

Implemented as a projection rebuilt from session bundles for each `mena list` invocation.
The projection is never persisted.

## Consequences

The index supports session discovery only and does not become canonical meeting-content storage.
