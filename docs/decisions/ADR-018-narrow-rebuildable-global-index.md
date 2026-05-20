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

## Consequences

The index supports session discovery only and does not become canonical meeting-content storage.
