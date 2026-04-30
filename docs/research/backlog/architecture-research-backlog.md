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
| R-01 | [Local Model Runtime Strategy](r-01-local-model-runtime-strategy.md) | Ready | Can run now. |
| R-02 | [Linux Audio Capture and Routing Strategy](r-02-linux-audio-capture-and-routing-strategy.md) | Ready | Can run now. Highest leverage for capture decisions. |
| R-03 | [Future OS Compatibility Implications](r-03-future-os-compatibility-implications.md) | Hold | Wait for `R-02`. |
| R-04 | [ASR Provider Strategy](r-04-asr-provider-strategy.md) | Ready | Can run now. |
| R-05 | [Diarization Provider Strategy](r-05-diarization-provider-strategy.md) | Ready | Can run now. |
| R-06 | [Fallback Provider Policy](r-06-fallback-provider-policy.md) | Ready | Can run now. |
| R-07 | [Install / Update / Uninstall Path for Local CLI Distribution](r-07-install-update-uninstall-path-for-local-cli-distribution.md) | Hold | Wait for `R-01`, `R-02`, `R-04`, and `R-05`. |
| R-08 | [Internal Protocol Implementation Strategy](r-08-internal-protocol-implementation-strategy.md) | Ready | Can run now. |
| R-09 | [Privacy-Preserving UX Telemetry Design](r-09-privacy-preserving-ux-telemetry-design.md) | Ready | Can run now. |

## Strict Execution Order

If you want the strict order that minimizes dependency churn, use:

1. `R-02 Linux Audio Capture and Routing Strategy`
2. `R-01 Local Model Runtime Strategy`
3. `R-04 ASR Provider Strategy`
4. `R-05 Diarization Provider Strategy`
5. `R-06 Fallback Provider Policy`
6. `R-08 Internal Protocol Implementation Strategy`
7. `R-09 Privacy-Preserving UX Telemetry Design`
8. `R-03 Future OS Compatibility Implications`
9. `R-07 Install / Update / Uninstall Path for Local CLI Distribution`

If you want the maximal parallel batch tonight without crossing known dependencies, spawn now:

- `R-02`
- `R-01`
- `R-04`
- `R-05`
- `R-06`
- `R-08`
- `R-09`

Hold:

- `R-03` until `R-02` produces a provisional Linux capture recommendation
- `R-07` until `R-01`, `R-02`, `R-04`, and `R-05` produce provisional runtime and dependency guidance

## Rule for New Research Tasks

An item belongs in this research backlog when:

- the architecture already depends on the answer
- the answer is not yet stable enough to hard-decide
- guessing during implementation would create avoidable rework or policy risk
