# ADR-036: Runtime profiles own stage-model selection

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-08-04
- **Superseded by:** —
- **Related:** #204, ADR-021, ADR-028, ADR-029, ADR-034

## Context

ADR-021 chose Ollama as the default PoC local-LLM runtime adapter, named an owned `llama.cpp` adapter as the first quality-validated alternative, and excluded vLLM from PoC scope. It also recorded a model-size/default-profile clause based on the hardware tiers and model evidence available in May 2026.

The implementation has since established a more precise ownership chain. The bundled runtime profile catalog declares the stage/provider/model combinations Mena currently supports. Accepted runtime-profile state records the machine and user choice selected from that catalog. Per-request provenance records the provider and model that actually executed. Keeping a model identifier or size tier as a permanent architecture default would duplicate that policy and make normal profile-catalog evolution require a new architecture decision.

The current bundled Ollama profiles select `llama3.1:8b` for both summarization and semantic review. ADR-034's references to that model record dated validation evidence and contract-risk context for the three-product note package; they do not need to make that identifier a permanent architecture constraint.

## Decision

Bundled stage-focused runtime profiles own supported stage-model selection.

- The bundled profile catalog records the current supported provider and model selection for each LLM-dependent text stage.
- Accepted runtime-profile state records the installation's machine/user choice from that catalog.
- Per-request provenance records the model that actually executed.
- Architecture decisions may cite exact model identifiers as dated validation evidence or explicit contract-risk context, but those citations do not establish architectural defaults unless a decision says that the identifier is itself part of a durable contract.
- Changing a bundled model selection is a runtime-policy and validation change. It does not require an architecture decision unless it also changes an architectural boundary, durable contract, or accepted policy owned by another decision.

This decision supersedes **only ADR-021's model-size/default-profile clause**. ADR-021 remains authoritative and unchanged for Ollama as the default PoC local-LLM runtime adapter, the owned `llama.cpp` alternative, and the exclusion of vLLM.

## Consequences

- `llama3.1:8b` remains the current bundled Ollama selection for both summarization and semantic review; this decision changes no runtime behavior.
- Runtime-profile catalog changes must carry the implementation and validation evidence appropriate to the affected stages without being mistaken for architecture changes.
- Accepted persisted profiles, provider routing, configuration semantics, schemas, and request provenance remain unchanged.
- ADR-034 remains unchanged. Its `llama3.1:8b` references continue to document the model used for the decision's dated validation and the known segment-grounding risk.
