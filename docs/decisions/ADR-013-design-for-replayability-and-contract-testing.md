# ADR-013: Design for replayability and contract testing

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-04-20
- **Superseded by:** —
- **Related:** —

## Context

Coding agents and CI pipelines need deterministic tests. Relying on live meetings for testing is unreliable and slow.

## Decision

Design for replayability and contract testing.

## Consequences

Coding agents can test core behavior with fixtures and fake providers instead of relying on live meetings.
