# ADR-001: Standalone local PoC

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-04-20
- **Superseded by:** —
- **Related:** —

## Context

Mena's architecture must be validated with a working product before considering integration with other systems. Building on top of an external platform (OpenClaw) in the PoC would add dependencies and constrain design choices before the core value proposition is proven.

## Decision

Build a standalone local PoC.

## Consequences

OpenClaw remains a future adapter/integration path, not a PoC dependency.
