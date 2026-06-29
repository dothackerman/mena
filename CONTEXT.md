# Mena Context

Mena is a privacy-first meeting notes product. This glossary defines product and architecture language that should stay stable across requirements, decisions, implementation, and review.

## Language

**LLM-dependent text stage**:
A stage that consumes meeting-derived text and uses an LLM to produce text-only judgment or output. In the current PoC, this means semantic coherence flagging and summarization.
_Avoid_: LLM task, remote task, AI step

**Route-bearing LLM text stage**:
An LLM-dependent text stage whose primary route and fallback profile list are governed by accepted runtime setup policy. For the current PoC, summarization is the only route-bearing LLM text stage; semantic review remains optional and outside the new fallback mechanism.
_Avoid_: all LLM stages, remote semantic review

**Stage-focused runtime profile**:
An accepted runtime choice for one processing stage, including its provider, model, fallback chain, and authorization policy. Profiles remain scoped to a stage whether the accepted provider is local Ollama or direct Mistral.
_Avoid_: global runtime profile, provider preference

**Summarization provider profile**:
An accepted provider choice for the summarization stage, including provider identity, model, device/runtime class, and policy. Ollama and Mistral are peer summarization providers behind the same Mena-owned summarization input and output contract.
_Avoid_: Mistral route subsystem, provider-specific summary schema

**Selectable primary route option**:
An eligible provider profile that setup may offer as the accepted primary for one stage. Mena may recommend local Ollama when available while still allowing the user to choose Mistral as the summarization primary when policy allows it.
_Avoid_: second primary runtime profile, remote fallback when primary

**Ordered fallback provider list**:
The accepted fallback provider profiles for a stage in priority order. Runtime tries the first viable and authorized fallback profile first; if that fallback cannot complete the stage, runtime may move to the next viable and authorized fallback profile.
_Avoid_: fallback depth, fallback boolean

**Direct Mistral summarization provider**:
The Mistral summarization provider behind Mena's existing summarization port. The provider owns Mistral API endpoint/request details and the fixed Mena-selected model; Mena owns the summarization input/output contract and setup/runtime authorization policy.
_Avoid_: Mistral route subsystem, OpenRouter route, user-configured Mistral endpoint
