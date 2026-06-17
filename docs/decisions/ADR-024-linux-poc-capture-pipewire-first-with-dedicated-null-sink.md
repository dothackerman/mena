# ADR-024: Linux PoC capture: PipeWire-first with dedicated null sink

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-05-01
- **Superseded by:** —
- **Related:** ADR-003

## Context

Capturing system audio on Linux requires routing it through a virtual device. PipeWire is the modern standard. A dedicated null sink per session isolates the meeting audio from other system sounds. Tiered fallback handles cases where PipeWire routing fails.

## Decision

Linux PoC capture realisation for `online_dual_stream` is **PipeWire-first with a session-scoped dedicated null sink**: explicit user confirmation of the meeting playback stream, move only that stream into the null sink, capture the null-sink monitor as the remote stream, separate loopback to the user's actual playback device, direct microphone capture as the local stream. The original fallback direction was tiered degraded capture with degradation events written to session metadata; the #83 installed-product slice narrows that fallback ladder in the amendment below and does not treat `mic_only` as successful online capture.

## Amendment: #83 online fallback narrowing (2026-06-17)

Issue #83 narrows the online fallback ladder for the PoC installed product:

- Planning-time absence of a microphone, playback stream, or output sink is a validation failure, not a degradation. Recording must not start until the user has selected or confirmed a valid online route plan.
- After a valid online route plan exists, setup-time or runtime failure of the dedicated meeting route may degrade to recording the selected output sink monitor as `system_audio` / `system_mix_remote`.
- After runtime degradation, #83 stays degraded until stop. Automatic recovery is deferred because the current `pw-record`/`pactl` route handoff would need to move the meeting playback stream away from the selected output monitor before recovery is proven.
- `mic_only` is not an acceptable fallback for `online_dual_stream` in #83. If neither the dedicated meeting route nor the selected output sink monitor can provide system audio, online capture fails or finalizes as partial while preserving already sealed chunks.
- In-person `in_person_single_mic` currently has no degraded capture mode. Loss of the selected room microphone is a failure or partial recording, not an automatic switch to another microphone.
- Degradation records are typed route/timeline facts connected to affected capture segments or elapsed recording time. They are not alternate audio-chunk containers; all sealed audio remains in `capture_metadata.segments`.

## Consequences

ADR-003 (one generic Capture Port with profiles) stands; this ADR pins the concrete Linux topology behind that port. Resolves R-02.
