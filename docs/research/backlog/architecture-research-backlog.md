# Architecture Research Backlog

Status: provisional backlog derived from architecture work

This document captures research tasks that surfaced during architecture work and must not be lost.
These are not yet implementation issues.
They exist because the architecture depends on their outcomes.

Companion architecture draft:

- [arc42 Working Draft](../../architecture/arc42-working-draft.md)

## Current Research Tasks

### R-01 Local Model Runtime Strategy

Question:

- how should local model-backed stages be accessed on user machines?

Current options to evaluate:

- direct library integration
- `Ollama`
- `LM Studio`
- other suitable local runtime approaches

Why it matters:

- affects installation complexity
- affects runtime operability
- affects portability to weaker tester machines
- affects how local and fallback providers are abstracted behind ports

### R-02 Linux Audio Capture and Routing Strategy

Question:

- what is the most reliable Linux approach for dual-stream capture and controlled capture-source routing?

Why it matters:

- affects recording robustness
- affects capture-profile design
- affects future in-person meeting support

### R-03 Future OS Compatibility Implications

Question:

- what parts of the chosen Linux capture approach create future cross-platform constraints?

Why it matters:

- architecture should not accidentally assume Linux-specific behavior in the core pipeline

### R-04 ASR Provider Strategy

Question:

- which ASR approach best fits the PoC and future product direction under sovereignty and operability constraints?

Why it matters:

- affects provider abstraction
- affects runtime profile
- affects output quality

### R-05 Diarization Provider Strategy

Question:

- which diarization approach best fits the PoC and future product direction under sovereignty and operability constraints?

Why it matters:

- affects processing reliability
- affects model/runtime dependencies
- affects later speaker-related features

### R-06 Fallback Provider Policy

Question:

- which remote fallback providers are acceptable under sovereignty constraints, and for which stages exactly?

Why it matters:

- affects policy enforcement
- affects tester operability on weaker machines
- affects what the user may or may not configure directly

### R-07 Install / Update / Uninstall Path for Local CLI Distribution

Question:

- what should the local install, upgrade, and uninstall path look like for a Linux CLI product used on the same machine as development and testing?

Why it matters:

- affects local operability
- affects repo-vs-runtime-data separation
- affects future packaging direction

### R-08 Internal Protocol Implementation Strategy

Question:

- which concrete transport and encoding approach best fits the private internal protocol?

Current candidates:

- `stdio`
- local socket
- JSON-based schema messages
- stronger schema tooling later if justified

Why it matters:

- affects process supervision
- affects contract testing
- affects implementation complexity

### R-09 Privacy-Preserving UX Telemetry Design

Question:

- how should product-quality telemetry be modeled so that it yields useful UX insight without exposing meeting substance, participant identity, or meaningful user identity?

Specific questions to answer:

- what events or aggregates are useful for product improvement?
- what identifiers are required, if any?
- how can telemetry stay useful without storing transcript text, meeting topic, participant names, or customer identity?
- should telemetry be local-only for the PoC, exportable on explicit user action, or never exported?

Why it matters:

- affects product improvement
- affects privacy guarantees
- affects future analytics structure
- affects whether telemetry becomes a cross-cutting concept later

## Rule for New Research Tasks

An item belongs in this research backlog when:

- the architecture already depends on the answer
- the answer is not yet stable enough to hard-decide
- guessing during implementation would create avoidable rework or policy risk
