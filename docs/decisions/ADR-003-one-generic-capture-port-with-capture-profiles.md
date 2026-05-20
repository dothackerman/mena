# ADR-003: One generic Capture Port with capture profiles

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-04-20
- **Superseded by:** —
- **Related:** ADR-024

## Context

Online dual-stream and in-person single-mic capture have different adapter behaviors but share core capture concepts. Separate ports would duplicate these concepts. A generic port with profiles lets both scenarios share the abstraction while differing in adapter implementation.

## Decision

Use one generic `Capture Port` with capture profiles.

## Consequences

`online_dual_stream` and `in_person_single_mic` can share core capture concepts while differing in adapter behavior.
