# ADR-010: Provider ports plus policy/capability resolution

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-04-20
- **Superseded by:** —
- **Related:** ADR-021, ADR-022, ADR-023, ADR-026

## Context

Different providers have different capabilities and policies. Workflow code should not hard-code which provider to use.

## Decision

Use provider ports plus policy/capability resolution.

## Consequences

Model/runtime implementations can vary without changing workflow logic; ZDR-bearing direct Mistral API is the PoC summarization fallback when local execution is insufficient.
