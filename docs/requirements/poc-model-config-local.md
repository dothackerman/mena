---
status: committed
phase: commitment
hierarchy: solution-requirement
babok-type: solution
# Traces from business requirement: draft-hardware-aware-model-profiles.md
created: 2026-05-23
author: PM (Kai)
committed-to: "Epic #16"
traces-to: "docs/requirements/draft-hardware-aware-model-profiles.md"
---

# Document Local Model Configuration for PoC

## Status

`[COMMITTED]`

## BABOK Classification

**Requirement type:** Solution requirement
**Hierarchy level:** Specific solution capability
**Parent business requirement:** Hardware-Aware Model Profiles (draft, elicitation phase)

## Context

As part of Epic #16 "Install, configure, and run mena on my Linux machine," the PoC needs a known-good model configuration for the target machine (Tuxedo). This is a narrow slice of the broader hardware-aware profiles concept, extracted for immediate implementation while the full concept remains in elicitation.

## Requirement

The installed mena product on the Tuxedo machine must have a documented, working configuration for:

1. ASR model (faster-whisper variant)
2. Diarization model (pyannote.audio variant)
3. Summarization model (Ollama model)
4. Semantic coherence model (Ollama model)

This configuration must be:
- Deterministic: same install always produces same model selection
- Documented: what models, why they were chosen, alternatives considered
- Smoke-testable: post-install verification confirms models load and run

## Acceptance Criteria

- [ ] `mena --version` reports installed version after fresh install
- [ ] `mena record --profile online_dual_stream --dry-run` completes without model errors
- [ ] Post-install smoke test reports model availability and loads selected models
- [ ] Configuration is documented alongside Epic #16 acceptance criteria

## Explicitly Out of Scope

- Hardware detection (beyond "this specific machine")
- User-configurable profiles
- Telemetry or performance measurement
- Model auto-update or version shifting
- Multiple hardware targets

## Traceability

| From | To | Relationship |
|------|----|-------------|
| Business requirement (draft) | This solution requirement | Is-slice-of |
| This solution requirement | Epic #16 | Is-captured-by |
| Epic #16 | #68 (implementation) | Is-implemented-by |

## Phase Transition Log

| Date | Phase | Notes | Author |
|------|-------|-------|--------|
| 2026-05-23 | Commitment | Extracted from elicitation during PM/Architect sync | Kai |
