# Architecture Research Backlog

Status: provisional backlog derived from architecture work

This document indexes research tasks that surfaced during architecture work and must not be lost.
These are not yet implementation issues.
They exist because the architecture depends on their outcomes.

Companion architecture overview:

- [Architecture Overview](../../architecture/architecture-overview.md)

## Current Research Tasks

| ID | Task | Status |
| --- | --- | --- |
| R-01 | [Local Model Runtime Strategy](r-01-local-model-runtime-strategy.md) | Open |
| R-02 | [Linux Audio Capture and Routing Strategy](r-02-linux-audio-capture-and-routing-strategy.md) | Open |
| R-03 | [Future OS Compatibility Implications](r-03-future-os-compatibility-implications.md) | Open |
| R-04 | [ASR Provider Strategy](r-04-asr-provider-strategy.md) | Open |
| R-05 | [Diarization Provider Strategy](r-05-diarization-provider-strategy.md) | Open |
| R-06 | [Fallback Provider Policy](r-06-fallback-provider-policy.md) | Open |
| R-07 | [Install / Update / Uninstall Path for Local CLI Distribution](r-07-install-update-uninstall-path-for-local-cli-distribution.md) | Open |
| R-08 | [Internal Protocol Implementation Strategy](r-08-internal-protocol-implementation-strategy.md) | Open |
| R-09 | [Privacy-Preserving UX Telemetry Design](r-09-privacy-preserving-ux-telemetry-design.md) | Open |

## Rule for New Research Tasks

An item belongs in this research backlog when:

- the architecture already depends on the answer
- the answer is not yet stable enough to hard-decide
- guessing during implementation would create avoidable rework or policy risk
