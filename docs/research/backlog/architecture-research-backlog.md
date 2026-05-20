# Architecture Research Backlog

Status: provisional backlog derived from architecture work

This document indexes research tasks that surfaced during architecture work and must not be lost.
These are not implementation issues.
They exist because the architecture depends on their outcomes.

Companion architecture overview:

- [Architecture Overview](../../architecture/architecture-overview.md)

## Research Task Operating Model

Each `R-xx` task is written as a self-sufficient brief for a deep research agent starting from zero product context.

That means:

- each task should contain enough product and architecture context to answer the question well
- the output is expected to land in `docs/research/notes/`
- the output is then consumed by human and architecture agents to refine the architecture
- tasks are snapshots of current architecture uncertainty, not timeless abstract questions

## Current Research Tasks

| ID | Task | Status | Notes |
| --- | --- | --- | --- |
| R-01 | [Local Model Runtime Strategy](r-01-local-model-runtime-strategy.md) | Done | Synthesised in [phase-1 synthesis §3.1](../synthesis-research-phase-1.md#31-r-01--local-model-runtime-strategy); decision recorded as `ADR-021`. |
| R-02 | [Linux Audio Capture and Routing Strategy](r-02-linux-audio-capture-and-routing-strategy.md) | Done | Synthesised in [phase-1 synthesis §3.2](../synthesis-research-phase-1.md#32-r-02--linux-audio-capture-and-routing-strategy); decision recorded as `ADR-024`. |
| R-03 | [Future OS Compatibility Implications](r-03-future-os-compatibility-implications.md) | Ready | Unblocked by R-02. ADR-024 gives R-03 a Linux baseline (PipeWire user-session, dedicated null sink, profile-aware capture port) to compare macOS/Windows analogues against. |
| R-04 | [ASR Provider Strategy](r-04-asr-provider-strategy.md) | Done | Synthesised in [phase-1 synthesis §3.3](../synthesis-research-phase-1.md#33-r-04--asr-provider-strategy); decision recorded as `ADR-022`. |
| R-05 | [Diarization Provider Strategy](r-05-diarization-provider-strategy.md) | Done | Synthesised in [phase-1 synthesis §3.4](../synthesis-research-phase-1.md#34-r-05--diarization-provider-strategy); decision recorded as `ADR-023`. |
| R-06 | [Fallback Provider Policy](r-06-fallback-provider-policy.md) | Done | Synthesised in [phase-1 synthesis §3.5](../synthesis-research-phase-1.md#35-r-06--fallback-provider-policy); decision recorded as `ADR-026`. |
| R-07 | [Install / Update / Uninstall Path for Local CLI Distribution](r-07-install-update-uninstall-path-for-local-cli-distribution.md) | Ready | Unblocked by R-01, R-02, R-04, R-05. ADR-021 (Ollama default), ADR-022 (`faster-whisper`/`whisper.cpp`), ADR-023 (`pyannote.audio`), ADR-024 (PipeWire user-session adapter) give R-07 the concrete dependency surfaces it needs. |
| R-08 | [Internal Protocol Implementation Strategy](r-08-internal-protocol-implementation-strategy.md) | Done | Synthesised in [phase-1 synthesis §3.6](../synthesis-research-phase-1.md#36-r-08--internal-protocol-implementation-strategy); decision recorded as `ADR-025`. Single-source (ChatGPT only); a future Nemotron run is not required. |
| R-09 | [Privacy-Preserving UX Telemetry Design](r-09-privacy-preserving-ux-telemetry-design.md) | Done | Synthesised in [phase-1 synthesis §3.7](../synthesis-research-phase-1.md#37-r-09--privacy-preserving-ux-telemetry-design); decision recorded as `ADR-027` (confirms `ADR-014` deferral, documents red lines). |

## Strict Execution Order

R-01, R-02, R-04, R-05, R-06, R-08, and R-09 are Done as of the [phase-1 synthesis](../synthesis-research-phase-1.md). The remaining order is:

1. `R-03 Future OS Compatibility Implications` — Ready
2. `R-07 Install / Update / Uninstall Path for Local CLI Distribution` — Ready

R-03 and R-07 may run in parallel; their dependencies are satisfied by the phase-1 architecture decisions (`ADR-021`–`ADR-026`).

## Rule for New Research Tasks

An item belongs in this research backlog when:

- the architecture already depends on the answer
- the answer is not yet stable enough to hard-decide
- guessing during implementation would create avoidable rework or policy risk
