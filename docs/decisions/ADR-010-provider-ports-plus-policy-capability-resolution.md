# ADR-010: Provider ports plus policy/capability resolution

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-04-20
- **Superseded by:** ADR-033 for direct Mistral summarization provider shape
- **Related:** ADR-021, ADR-022, ADR-023, ADR-026, ADR-033

## Context

Different providers have different capabilities and policies. Workflow code should not hard-code which provider to use.

## Decision

Use provider ports plus policy/capability resolution.

## Consequences

Model/runtime implementations can vary without changing workflow logic. Per ADR-033, direct ZDR Mistral is a normal summarization provider behind the summarization port and may be accepted as primary, fallback, or only viable provider when the summarization stage policy allows it.
