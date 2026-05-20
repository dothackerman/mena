# ADR-011: Version durable schemas and internal protocol from day one

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-04-20
- **Superseded by:** —
- **Related:** ADR-025

## Context

Schemas and protocols will evolve. Without versioning from the start, breaking changes are invisible and migration becomes accidental.

## Decision

Version durable schemas and internal protocol from day one, treating the PoC as the initial `v1` contract epoch without adding a formal migration framework yet.

## Consequences

Persisted contracts start with real schemas or version markers and evolve independently by contract; protocol initialization includes version agreement and minimal capabilities; migrations remain explicit future work, not accidental hidden behavior.
