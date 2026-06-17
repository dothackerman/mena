# ADR-032: Continuous capture with rotating source segments

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-06-17
- **Superseded by:** —
- **Related:** #83, ADR-003, ADR-004, ADR-024

## Context

Issue #83 turns `mena record` from a session-record scaffold into real Linux audio capture for the PoC. The recorder must support meeting-length capture, write ordered sealed source segments while recording, and bound crash or interruption loss to the current unsealed segment.

The implementation has two different lifecycles:

1. **Capture lifecycle:** connect to the selected audio source and continuously receive PCM frames.
2. **File lifecycle:** seal durable source chunks at fixed segment boundaries.

Tying these together by starting a fresh `pw-record` process for each 30-second segment would repeatedly tear down and reconnect the audio graph. That creates boundary risk at every segment: process startup latency, graph relinking, route races, timeout/kill ambiguity, and fresh dual-source alignment offsets.

PipeWire's native model is a long-lived stream in the audio graph. `pw-record` is a simple capture tool that can write raw output to stdout, but it does not provide a rolling segment contract. GStreamer's file-splitting sinks model the stronger long-term shape: a live pipeline whose sink rotates output files.

## Decision

Mena's source-capture contract is **continuous capture with rotating persistence**:

- a logical capture source owns a long-lived capture stream/process for the recording attempt;
- the capture adapter emits raw PCM frames to a Mena-owned segment writer;
- the segment writer seals independently readable WAV chunks at fixed frame-count boundaries;
- fallback and recovery decisions happen at segment boundaries so a sealed chunk has one effective capture mode;
- already sealed chunks are durable and processable;
- the current unsealed chunk may be sealed on stop when enough aligned PCM exists; otherwise it remains unpersisted and the session-level finalization or failure record carries the partial outcome;
- processing consumes ordered, verified, sealed source segments, not live capture implementation details.

For issue #83, the PoC implementation uses long-lived `pw-record --raw -` subprocesses per selected logical source and a Python-owned PCM/WAV segment writer. This keeps the correct lifecycle boundary without introducing GStreamer integration into the first live-capture slice.

Issue #83 implements the amended ADR-024 online fallback semantics:

- no planning-time degraded fallback is invented; missing or invalid route choices are pre-recording validation failures;
- `online_dual_stream` primary system audio is the selected meeting playback stream isolated through the dedicated Mena route;
- if the primary route fails during setup or runtime, Mena may degrade to the selected output sink monitor, which is persisted as system-mix remote audio rather than clean selected meeting audio;
- if the primary route is already a general system/output monitor, there is no lower-quality online fallback for #83;
- `mic_only` is not a valid online fallback for #83;
- `in_person_single_mic` has no degraded mode in #83; selected microphone loss is a failure or partial recording.

Degradations are typed route/timeline events, not audio artifacts. All sealed audio chunks remain in `capture_metadata.segments`; segment metadata must identify the effective capture mode and route where it matters. Degradation records must be linkable to the meeting timeline by elapsed seconds, segment index, or both, and user-visible messages should be derived from typed codes rather than stored as the only source of truth.

GStreamer/PyGObject is the intended future adapter family when Mena needs stronger media-pipeline ownership, richer bus/error handling, more explicit timing/caps diagnostics, or better multi-source coordination. That future adapter must remain behind the same capture port and session-record contract; it must not leak GStreamer-specific concepts into durable session artifacts.

## Consequences

- `pw-record` subprocess lifetime is the source-capture lifetime, not the segment lifetime.
- Segment duration is enforced by frame counts, not by killing a recorder process at a wall-clock timeout.
- Route selection, sample rate, channels, sample format, byte counts, checksums, and segment ordering remain Mena-owned persisted facts.
- Degraded system-mix capture remains processable, but downstream artifacts must preserve provenance that the source was a selected output mix rather than clean isolated meeting audio.
- The current adapter can be replaced by a GStreamer/PyGObject adapter without changing `session-record.v1` or downstream processing contracts.
- The current `pw-record` adapter is Linux/POSIX-specific and uses pipe readiness polling around stdout; this is acceptable inside the Linux PipeWire adapter and must not leak into the core capture contract.
- Per-segment `incomplete` or `lost` statuses are not produced by the #83 PoC adapter. They are reserved for a future adapter/runtime that can distinguish and persist a partial source fragment as a first-class segment artifact.
- Review and smoke tests must check for contiguous frame counts, independently readable chunks, prior sealed chunk preservation after stop/failure, and stable dual-source offset across segment boundaries.
