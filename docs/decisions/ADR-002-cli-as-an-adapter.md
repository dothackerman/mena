# ADR-002: CLI as an adapter

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-04-20
- **Superseded by:** —
- **Related:** —

## Context

The CLI is the primary user interface for the PoC, but workflow logic must remain independent of how the user interacts with the system. Treating the CLI as a first-class adapter preserves the ability to add GUIs, APIs, or agent interfaces later without refactoring core logic.

## Decision

Treat the CLI as an adapter.

## Consequences

Core workflow logic must not depend on command-line control flow, prompts, or terminal behavior.
