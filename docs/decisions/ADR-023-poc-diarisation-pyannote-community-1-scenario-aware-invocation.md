# ADR-023: PoC diarisation: pyannote community-1, scenario-aware invocation

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-05-01
- **Superseded by:** —
- **Related:** ADR-003, PDR-001

## Context

Diarisation quality varies by scenario. In online dual-stream, the local mic already has structural attribution. Running diarisation on the combined dual-stream is invalid because community-1 requires mono input.

## Decision

PoC diarisation adapter is **`pyannote/speaker-diarization-community-1`**, invoked **scenario-aware**: not on the local mic in `online_dual_stream`, on the remote/system stream when value-adding, on the room mic for `in_person_single_mic`. Confidence is a product-level heuristic layer, not a model signal.

## Consequences

Combined dual-stream is never fed to the diarizer because `community-1` is mono-only. Three artifacts persisted per session (regular timeline, exclusive projection, correction overlay). Resolves R-05.
