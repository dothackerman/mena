# ADR-020: Per-session speaker anchor artifacts for REQ-F18

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-04-20
- **Superseded by:** —
- **Related:** —

## Context

Cross-session speaker matching (REQ-F18) is deferred, but the data needed for it must be collected during the PoC.

## Decision

Persist per-session speaker anchor artifacts for `REQ-F18`.

## Implementation status

Accepted but not yet implemented.
Implementation is deliberately deferred under the smallest-slice guardrail; no speaker-anchor artifact code exists in the current PoC.

## Consequences

Corrected speaker labels and associated embeddings are stored inside session bundles for forward compatibility, while cross-session matching remains deferred.
