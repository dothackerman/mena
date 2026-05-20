# ADR-006: Per-invocation orchestrator as canonical Session Record writer

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-04-20
- **Superseded by:** —
- **Related:** ADR-004, ADR-007

## Context

Session truth must be authoritative and consistent. If workers independently mutate session state, cross-referencing and integrity verification become unreliable.

## Decision

Make the per-invocation orchestrator the canonical `Session Record` writer while it holds session authority.

## Consequences

Capture workers and future workers emit facts/events/artifact references instead of independently mutating session truth; cross-invocation continuity is reconstructed from files.
