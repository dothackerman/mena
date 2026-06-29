# ADR-033: Mistral summarization provider and ordered fallback

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-06-28
- **Superseded by:** -
- **Related:** ADR-021, ADR-026, ADR-029, ADR-031, PDR-004

## Context

ADR-026 accepted a direct ZDR Mistral API path for summarization under operator-attested contractual predicates. This decision turns that into a concrete PoC implementation shape.

Mena remains local by default. Recording, transcription, diarization, audio, raw files, speaker embeddings, speaker-sensitive artifacts, and semantic review are outside the remote Mistral route. Semantic review remains optional/local unless a later product decision makes it mandatory or route-bearing.

Mistral is a new summarization provider at the same provider-port level as Ollama. It must not create a parallel summarization contract, payload-selection path, artifact schema, or provenance subsystem.

## Decision

Implement Mistral as a normal provider behind the existing summarization port.

The summarization port remains Mena-owned:

- the input content sent to a provider is the existing summarization request content;
- the summary output contract is the existing Mena `summary.v1` / `SummaryResult` schema;
- the persisted summary artifact records provider metadata through the existing provider metadata field;
- summary reuse remains scoped to provider configuration, as it already is for Ollama.

The Mistral provider owns Mistral-specific implementation details:

- the direct Mistral API endpoint/path it uses;
- the request envelope required by Mistral;
- the strongest supported structured-output mechanism for Mena's fixed summary schema;
- API-key validation/readiness behavior;
- Mistral API error classification;
- bounded retries where Mistral response semantics make retry safe;
- the fixed Mistral model selected by Mena.

The implementation must not expose user-configurable Mistral endpoints or arbitrary Mistral model IDs. The user supplies a Mena-scoped API key, but endpoint and model use are provider/policy concerns. Mena selects the fixed model outside user configuration.

## Setup And Runtime Profile

Setup/runtime profile state should stay close to the existing provider profile model. A summarization profile names the provider, model, device, and policy. Mistral appears as a summarization provider/profile entry, not as a special workflow.

Runtime profile state may extend to represent an ordered fallback list for summarization. The accepted primary summarization provider remains in the existing stage policy shape. Accepted fallback providers live in a separate ordered fallback-provider list. Runtime consumes only that accepted order; it does not invent fallback based on provider identity.

Implemented list shapes are intentionally small:

- Mistral as accepted primary in `stage_policies`, with an empty fallback list;
- local summarization primary in `stage_policies`, with Mistral as the first accepted fallback provider;
- local summarization primary in `stage_policies`, with an empty fallback list.

Local LLM CPU fallback is not added by this decision. Controlled local Ollama CPU fallback should reuse the ordered fallback-provider list by adding an accepted local CPU Ollama provider/profile entry when that capability exists. Multi-step chains such as local GPU -> local CPU -> Mistral are future-valid only after the local CPU provider profile exists.

Using the accepted primary provider does not require a runtime prompt. This is existing accepted-primary behavior, not Mistral-specific prompting logic. Runtime prompts apply only when Mena needs to use a fallback transition that was not pre-authorized. Local fallback authorization never authorizes a later remote fallback.

## Provider Readiness And Errors

The Mistral provider owns readiness checks. The implementation must explicitly inspect Mistral's current API capabilities and choose the narrowest useful readiness check, such as key presence plus an API-supported authentication/model/readiness probe, or a minimal completion smoke test if no safer check exists.

The Mistral provider also owns Mistral API error mapping. The implementation must read the current Mistral API docs and map errors into Mena's existing stage semantics:

- retryable transient provider failure;
- provider unavailable, allowing accepted fallback when configured and authorized;
- hard failure for contract/schema errors, authentication/authorization problems, quota/funds exhaustion, or ambiguous partial-success states where retry would be unsafe.

The implementation should not ask users or setup state to choose endpoints. Endpoint selection is provider-owned. If the provider cannot call Mistral successfully, it reports provider unavailable or a hard provider error according to the API response.

## Privacy And Account Posture

Mena can enforce only its technical perimeter:

- direct Mistral provider, not a broker;
- Mena-owned request content and summary schema;
- text-only summarization request;
- Mistral's default direct API endpoint only; no explicit US endpoint or brokered route;
- fixed Mena-selected model;
- Mena-scoped API key source such as `MENA_MISTRAL_API_KEY`;
- no API-key persistence in setup state, config, logs, provenance, or errors;
- provider/model/provenance recording through existing artifact and runtime provenance contracts.

The user/operator owns the Mistral account, workspace, API key, subscription, ZDR activation, legal terms, funds/quota, and commercial posture. Mena may present a setup acknowledgement for that account posture, but must not claim it verified ZDR, plan tier, training opt-out, subprocessor posture, legal isolation, or account funds.

Setup disclosure is a product responsibility governed by `PDR-004`. At minimum, selecting Mistral sends summarization text to the user's configured Mistral account, may incur Mistral API charges, depends on the user's account/ZDR posture, may be inappropriate for meetings with special-category or similarly sensitive personal data unless the operator has confirmed the account and legal posture, and will be recorded in provenance when used.

## Consequences

- ADR-026 is superseded as the active implementation shape, but its operator-attested ZDR reasoning still carries forward as product/policy context.
- The Mistral provider reuses the existing summarization port, `summary.v1` artifact, provider metadata, provider config identity, and runtime profile provenance.
- The implementation must not introduce Mistral-specific payload selection, artifact schemas, or provenance subsystems.
- The implementation must extend fallback ordering only as far as needed for accepted summarization provider/profile lists.
- The implementation must include Mistral API discovery during implementation for readiness checks, structured output support, retry classification, and error mapping.
- The implementation must include vertical-slice tests and UAT scenarios covering Mistral primary, Mistral fallback, provoked fallback, missing API key, invalid API key, no internet/provider unavailable, and quota/funds failure where Mistral exposes a recognizable signal.
