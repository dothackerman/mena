# ADR-009: Recording, transcription, and diarization local-only

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-04-20
- **Superseded by:** —
- **Related:** ADR-026

## Context

Audio recordings contain sensitive meeting content. Sending them to a remote service for transcription or diarization violates the sovereignty principle.

## Decision

Make recording, transcription, and diarization local-only.

## Consequences

No audio leaves the user's machine due to `mena`; quality/runtime tradeoffs must be solved locally for these stages.
