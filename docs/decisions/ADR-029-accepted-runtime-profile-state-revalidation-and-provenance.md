# ADR-029: Accepted runtime profile state, revalidation, and provenance

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-06-08
- **Superseded by:** -
- **Related:** #40, #72, ADR-010, ADR-012, ADR-021, ADR-022, ADR-023, ADR-028

## Context

`ADR-028` separates the bundled profile catalog, local installation state, and user or administrator choice. Issue #72 completes the next installed-product contract needed for #40 UAT: setup must not merely print that local providers look ready. It must persist which runtime profile the user accepted, and processing must use and prove that profile later.

Without accepted runtime profile state, `mena process` and `mena summarize` can drift back to provider defaults, silently change devices or models, and produce artifacts that look like they used setup choices when they did not. GPU availability, local model caches, provider package readiness, and local services can also change between setup and derived artifact generation.

Recording is separate from processing. A temporary processing-resource problem, such as unavailable GPU VRAM, must not prevent recording a meeting. It must prevent misleading derived processing artifacts unless the user explicitly allows a supported fallback for that run.

## Decision

Mena persists accepted runtime setup as product state under `RuntimePaths.state_home`, not in the repository and not in user-authored configuration. The PoC state file is `setup-state.json`.

The persisted state records:

- schema version and bundled catalog version
- accepted profile id and version
- product version and accepted timestamp
- stage provider/model/device policy
- selected ASR profile details
- setup readiness summary
- structured runtime evidence and a digest of that evidence

Setup writes accepted state only after:

1. local runtime evidence is gathered without mutation
2. evidence is evaluated against the bundled profile catalog
3. planned automated and manual actions are presented
4. the user confirms setup mutation where mutation is needed
5. readiness is rechecked and complete

Processing and standalone summarization perform a lightweight runtime revalidation before provider construction and before derived stages. They load the accepted setup state, verify catalog/profile compatibility, gather current local evidence, evaluate the accepted profile, and reject stale state before producing derived artifacts if required facts no longer match.

Default provider construction must come from the accepted or explicit fallback runtime profile. Workflow code must not fall back to provider defaults when an accepted profile exists.

Fallback is explicit per run for the PoC:

- interactive or scripted fallback is not silent
- non-interactive processing fails unless the command carries an explicit fallback flag
- fallback can only use a supported fallback profile that current evidence satisfies
- provenance records accepted profile, actual profile, stage-level provider/model/device policy, whether fallback was used, and the fallback status for the run

Derived artifact commands persist run-level profile provenance as a session artifact. Stage artifacts continue to persist provider/model/device metadata through their provider contracts.

## Consequences

Positive:

- #40 UAT can answer which installed product version, accepted profile, actual profile, provider, model, and device produced output.
- Processing and standalone summarization no longer silently ignore setup decisions by constructing default providers.
- Stale setup conditions stop before misleading derived processing artifacts are produced.
- Recording remains independently useful even when processing resources are unavailable.

Tradeoffs:

- Derived artifact commands now depend on accepted setup state when using the real installed-product providers.
- Unit tests that exercise processing mechanics with fake providers should inject those providers explicitly rather than relying on local runtime setup.
- The first PoC fallback is intentionally narrow and bundled; external profile catalogs, broad recommendation UX, and doctor/re-setup flows remain out of scope.

Deferred:

- External or updateable runtime profile catalogs.
- Desktop setup UX.
- Telemetry-driven recommendations.
- Remote ASR or diarization setup.
- Future consumer-product fallback policy beyond the explicit PoC flag.
