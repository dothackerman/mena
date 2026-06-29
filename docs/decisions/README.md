# Decision Records

This directory contains all decision records for the mena project. Decisions are immutable once accepted — they are superseded, not edited.

## Types

| Prefix | Owner | Focus |
|--------|-------|-------|
| **ADR** | Architect | Structural decisions — component boundaries, interfaces, technology choices, integration patterns |
| **PDR** | PM | Product decisions — user-facing behavior, scope constraints, experience rules |

Both types can reference each other for traceability. References are optional, not mandatory.

## Status Lifecycle

```
Proposed → Accepted → [Superseded | Deprecated | Rejected]
```

- **Proposed** — drafted, not yet agreed
- **Accepted** — agreed and active
- **Deferred** — accepted but implementation delayed; may include conditions for reactivation
- **Superseded** — replaced by a new decision. Must reference the successor.
- **Deprecated** — no longer relevant. Must include reason.
- **Rejected** — never accepted. Kept for the record.

**Decisions are never deleted.**

## Template

```markdown
# [ADR|PDR]-NNN: [title]

- **Type:** Architecture | Product
- **Status:** Proposed | Accepted | Deferred | Superseded | Deprecated | Rejected
- **Date:** YYYY-MM-DD
- **Superseded by:** [ADR-NNN | PDR-NNN | —]
- **Related:** #issue, PR #nn, ADR-NNN, PDR-NNN

## Context

[Why we're making this decision. What forces are at play?]

## Decision

[What we decided. Be specific and unambiguous.]

## Consequences

[What this means — positive and negative. What becomes easier or harder?]
```

## Index

| # | Type | Title | Status | Date |
|---|------|-------|--------|------|
| ADR-001 | Architecture | Standalone local PoC | Accepted | 2026-04-20 |
| ADR-002 | Architecture | CLI as an adapter | Accepted | 2026-04-20 |
| ADR-003 | Architecture | One generic Capture Port with capture profiles | Accepted | 2026-04-20 |
| ADR-004 | Architecture | Session Orchestrator Process and Capture Worker Process | Accepted | 2026-04-20 |
| ADR-005 | Architecture | Explicit criteria before adding more processes | Accepted | 2026-04-20 |
| ADR-006 | Architecture | Per-invocation orchestrator as canonical Session Record writer | Accepted | 2026-04-20 |
| ADR-007 | Architecture | Files for durable artifacts, protocol for live coordination | Accepted | 2026-04-20 |
| ADR-008 | Architecture | Raw audio sealed, corrections separate from transcript output | Accepted | 2026-04-20 |
| ADR-009 | Architecture | Recording, transcription, and diarization local-only | Accepted | 2026-04-20 |
| ADR-010 | Architecture | Provider ports plus policy/capability resolution | Accepted | 2026-04-20 |
| ADR-011 | Architecture | Version durable schemas and internal protocol from day one | Accepted | 2026-04-20 |
| ADR-012 | Architecture | Runtime data outside repository, preserve session data on uninstall | Accepted | 2026-04-20 |
| ADR-013 | Architecture | Design for replayability and contract testing | Accepted | 2026-04-20 |
| ADR-014 | Architecture | Defer privacy-preserving UX telemetry | Deferred | 2026-04-20 |
| ADR-015 | Architecture | Output quality and CLI friction as PoC learning outcomes | Accepted | 2026-04-20 |
| ADR-016 | Architecture | Include both capture scenarios in CLI PoC scope | Superseded | 2026-04-20 |
| PDR-002 | Product | Include both online meeting and in-person single-mic modes in PoC CLI scope | Accepted | 2026-05-21 |
| PDR-003 | Product | Record-time language is the session source of truth | Accepted | 2026-06-16 |
| ADR-017 | Architecture | Versioned manual-notes input artifacts for summarization | Accepted | 2026-04-20 |
| ADR-018 | Architecture | Narrow rebuildable global index | Accepted | 2026-04-20 |
| ADR-019 | Architecture | Centralize error recovery authority in the active orchestrator | Accepted | 2026-04-20 |
| ADR-020 | Architecture | Per-session speaker anchor artifacts for REQ-F18 | Accepted | 2026-04-20 |
| ADR-021 | Architecture | PoC local-LLM runtime: Ollama default, owned llama.cpp alternative | Accepted | 2026-05-01 |
| ADR-022 | Architecture | PoC ASR: faster-whisper with hardware-selected Whisper profiles | Accepted | 2026-05-01 |
| ADR-023 | Architecture | PoC diarisation: pyannote community-1, scenario-aware invocation | Accepted | 2026-05-01 |
| ADR-024 | Architecture | Linux PoC capture: PipeWire-first with dedicated null sink | Accepted | 2026-05-01 |
| ADR-025 | Architecture | PoC internal protocol: stdio + newline-delimited JSON | Accepted | 2026-05-01 |
| ADR-026 | Architecture | ZDR-bearing Mistral API fallback: operator-attested predicates | Superseded | 2026-05-01 |
| ADR-027 | Architecture | UX telemetry absent in PoC v0 | Deferred | 2026-05-01 |
| ADR-028 | Architecture | Installation profiles, local setup state, and preflight separation | Accepted | 2026-05-23 |
| ADR-029 | Architecture | Accepted runtime profile state, revalidation, and provenance | Accepted | 2026-06-08 |
| ADR-030 | Architecture | Local runtime resource stewardship for post-meeting processing | Accepted | 2026-06-15 |
| ADR-031 | Architecture | Runtime fallback authorization and setup state | Accepted | 2026-06-15 |
| ADR-032 | Architecture | Continuous capture with rotating source segments | Accepted | 2026-06-17 |
| ADR-033 | Architecture | Mistral summarization provider and ordered fallback | Accepted | 2026-06-28 |
| PDR-001 | Product | Automatic correction overlay selection | Accepted | 2026-05-20 |
| PDR-004 | Product | Mistral summarization disclosure and consent | Accepted | 2026-06-29 |
