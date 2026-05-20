# ADR-022: PoC ASR: faster-whisper with hardware-selected Whisper profiles

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-05-01
- **Superseded by:** —
- **Related:** ADR-010

## Context

ASR quality directly affects user trust. faster-whisper provides good quality with GPU acceleration. whisper.cpp is needed for weaker/brittle hardware. Model size must match available hardware.

## Decision

PoC primary ASR adapter is **`faster-whisper`** with hardware-selected Whisper-family profiles; `large-v3-turbo` is the quality ceiling for strong machines, `large-v3` is an explicit quality rerun, and **`whisper.cpp`** is the first fallback adapter for weaker/brittle hardware. Provider metadata (checkpoint, decode parameters) is persisted with every transcript.

## Consequences

ASR output is treated as provisional and replayable. Installation/preflight chooses a suitable model-size configuration from available VRAM/RAM and adapter capability. Vocabulary support uses `initial_prompt` and hotwords (soft biasing); deterministic vocabulary control is out of scope. Resolves R-04.
