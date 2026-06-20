# ADR-030: Local runtime resource stewardship for post-meeting processing

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-06-15
- **Superseded by:** -
- **Related:** #78, ADR-010, ADR-021, ADR-022, ADR-023, ADR-029, ADR-031

## Context

Issue #78 showed that provider readiness is not enough to prove that the local post-meeting workflow is schedulable on constrained hardware. During UAT, `llama3.2:3b` could remain GPU-resident after an LLM stage and later starve GPU-heavy diarization on a 6 GB GPU.

The existing architecture already persists accepted runtime profile state and explicit fallback provenance through `ADR-029`. The missing decision is how Mena should manage live local runtime resources during post-meeting processing without becoming a blunt system-service manager.

The product goal is not maximum speed at all costs. For post-meeting work, Mena should prefer checkpoint-safe completion over speed when a supported slower local path exists.

## Decision

Mena treats post-meeting processing as a resource-managed local workflow.

The active orchestrator owns recovery decisions. Provider adapters report typed failures and resource context; they do not independently choose fallback policy.

Mena may actively steward only resources it owns or has just used:

- keep `faster-whisper` warm during active transcription work, then release it after transcription reaches a terminal boundary
- keep pyannote warm during active diarization work, then release it after diarization reaches a terminal boundary
- keep Ollama model residency warm during contiguous Mena-owned LLM work, then release Mena-owned model residency at terminal LLM boundaries
- never stop the Ollama daemon as normal product behavior
- never unload arbitrary local LLM model runners that Mena cannot reasonably claim it loaded or used

For the current CLI PoC, terminal LLM boundaries include:

- the end of `mena process`, because correction and summarization may happen later or not at all
- the end of `mena summarize`

A future contiguous command that runs semantic review and summarization as one active workflow may hold the same local LLM lease across both stages, then release it at the final summarization boundary.

Primary runtime remains preferred, but retry is evidence-based:

- Mena should try the accepted primary runtime when current evidence says it is plausible
- Mena should not repeatedly trigger known out-of-memory failures merely to prove primary was attempted
- when primary GPU execution is blocked, Mena may release Mena-owned resources, recheck live headroom, then retry primary or use an accepted fallback according to policy

Fallback granularity for #78 is per source/artifact, not per arbitrary audio chunk:

- transcription fallback applies to a source transcript artifact
- diarization fallback applies to a source diarization artifact
- mixed primary/fallback output inside a single artifact requires explicit per-segment provenance and is out of scope for #78

For the PoC, local fallback is constrained:

- ASR and diarization may use an accepted local CPU fallback profile when authorized
- placement fallback using the same model/checkpoint is preferred when feasible
- profile fallback that changes model size or checkpoint is allowed only when the accepted fallback profile says so and provenance records the actual provider/model/device
- Ollama has no controlled CPU fallback in the PoC; Mena may release owned resources and retry, but if the local LLM stage still cannot run and no supported fallback exists, that stage follows its stage policy

Semantic review is a quality-enhancement stage for correction, not the sole path to correction. If local semantic review cannot run after resource release and no supported fallback exists, processing may complete with semantic review explicitly skipped or degraded, provided confidence-based review flags and machine-readable diagnostics remain available.

Summarization is different: if local summarization cannot run and no approved summarization fallback is available, summarization fails with an actionable resource error.

All fallback, skip, degradation, and resource-release outcomes must be provenance-backed. The user-facing message should explain what Mena tried, what blocked completion on the primary path, what fallback or degradation was used, and what choices remain.

## Consequences

Positive:

- Mena can complete more post-meeting workflows on constrained local machines.
- GPU pressure from Mena-owned local LLM residency is handled at stage boundaries instead of by stopping system services.
- Runtime fallback remains checkpoint-safe and traceable.
- Semantic review failures no longer have to block human correction when other review signals exist.

Tradeoffs:

- Runtime orchestration becomes responsible for live resource stewardship, not only provider construction.
- Setup/runtime profile state must describe accepted fallback capability and authorization more explicitly.
- Implementation must avoid tight retry loops and must distinguish Mena-owned residency from unrelated local runtime state.
- Some fallback paths may trade quality for completion; provenance must make that visible.

Deferred:

- Mixed-provider provenance inside one transcript or diarization artifact.
- A controlled local LLM CPU fallback profile.
- Remote fallback for ASR, diarization, or semantic review.
- Desktop UX for resource leases and automatic fallback explanation.
