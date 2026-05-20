# ADR-007: Files for durable artifacts, protocol for live coordination

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-04-20
- **Superseded by:** —
- **Related:** ADR-006, ADR-025

## Context

Raw audio and generated artifacts are large. Transporting them through the inter-process protocol would bloat it and complicate recovery.

## Decision

Use files for durable artifacts and protocol for live coordination.

## Consequences

The protocol stays small and avoids transporting raw audio or large generated artifacts.
