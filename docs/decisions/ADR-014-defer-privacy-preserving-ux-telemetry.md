# ADR-014: Defer privacy-preserving UX telemetry

- **Type:** Architecture
- **Status:** Deferred
- **Date:** 2026-04-20
- **Superseded by:** —
- **Related:** ADR-027

## Context

Telemetry is needed for product improvement but implementing it without a clear privacy model risks collecting data that should not be collected.

## Decision

Defer privacy-preserving UX telemetry until research provides a concrete model.

## Consequences

Telemetry must not be implemented ad hoc or mixed into session content artifacts.
