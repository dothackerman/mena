# ADR-004: Session Orchestrator Process and Capture Worker Process

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-04-20
- **Superseded by:** —
- **Related:** ADR-005, ADR-006

## Context

Capture is a real-time activity that must survive downstream failures. The orchestrator manages workflow, session state, and user interaction. Separating capture into its own process gives it failure isolation and real-time ownership without making every workflow step a separate service.

## Decision

Use a `Session Orchestrator Process` and a `Capture Worker Process`.

## Consequences

Capture has failure isolation and real-time ownership without turning every workflow step into a separate service.
