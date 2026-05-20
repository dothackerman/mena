# ADR-008: Raw audio sealed, corrections separate from transcript output

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-04-20
- **Superseded by:** —
- **Related:** ADR-009, PDR-001

## Context

If corrections modify the generated transcript in-place, the original output is lost and cannot be compared or regenerated. Raw audio must remain an immutable source of truth.

## Decision

Keep raw audio sealed and separate corrections from generated transcript output.

## Consequences

Review transparency is preserved and transcripts can be regenerated from source audio when needed.
