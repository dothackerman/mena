# ADR-024: Linux PoC capture: PipeWire-first with dedicated null sink

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-05-01
- **Superseded by:** —
- **Related:** ADR-003

## Context

Capturing system audio on Linux requires routing it through a virtual device. PipeWire is the modern standard. A dedicated null sink per session isolates the meeting audio from other system sounds. Tiered fallback handles cases where PipeWire routing fails.

## Decision

Linux PoC capture realisation for `online_dual_stream` is **PipeWire-first with a session-scoped dedicated null sink**: explicit user confirmation of the meeting playback stream, move only that stream into the null sink, capture the null-sink monitor as the remote stream, separate loopback to the user's actual playback device, direct microphone capture as the local stream. Tiered fallback: `mic + default_sink_monitor` → `mic_only`, with degradation events written to session metadata.

## Consequences

ADR-003 (one generic Capture Port with profiles) stands; this ADR pins the concrete Linux topology behind that port. Resolves R-02.
