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

**Application workflow module**:
A module that owns one user-meaningful workflow (record, process, correct, summarize, export) behind an interface that is independent of any user interface. Interface adapters (CLI today, GUI/agent later) delegate to workflow modules and own only argument parsing, wording, rendering, and exit codes. The interface of a workflow module is its test surface; tests must not need to go through an interface adapter to exercise workflow behavior.
_Avoid_: CLI command implementation, service class, manager, handler

**Interaction port**:
The seam through which an application workflow module asks the user for decisions (choices, confirmations) and reports notices, expressed semantically rather than as terminal text. The workflow owns which decision is needed and when; the interface adapter owns prompt wording, rendering, and input mechanics. Test adapters script answers at this seam instead of monkeypatching.
_Avoid_: input_fn, prompt callback, print hook

**Capability resolver**:
The single module that turns a processing stage plus the accepted stage-focused runtime profile state into a bound, authorized provider. It owns eligibility, authorization checks, re-selection from the ordered fallback provider list when a provider reports unavailable, bounded retry, and route provenance recording. Workflow modules ask the resolver for a working provider; they do not select, validate, or fall back between providers themselves.
_Avoid_: provider factory helper, runtime fallback dance, per-command provider selection

**Direct Mistral summarization provider**:
The Mistral summarization provider behind Mena's existing summarization port. The provider owns Mistral API endpoint/request details and the fixed Mena-selected model; Mena owns the summarization input/output contract and setup/runtime authorization policy.
_Avoid_: Mistral route subsystem, OpenRouter route, user-configured Mistral endpoint

**Note package**:
The structured-notes output of one accepted summarization run: exactly three note products — timestamped timeline, action items, and general summary. A package is accepted and persisted as a whole; there is no `important_points` product.
_Avoid_: summary artifact, monolithic summary, summary fields

**Note product**:
One user-meaningful notes output inside a note package, generated under its own prompt and output contract with its own provenance. The three note products are the timestamped timeline, the action items, and the general summary.
_Avoid_: summary section, summary field, sub-summary

**Timestamped timeline**:
The chronological note product: a record of what happened in the meeting, each entry grounded in the transcript segments it came from, with its timestamp derived from those segments rather than authored by the model.
_Avoid_: timeline strings, chronology summary, bare dates

**General summary**:
The recall note product: a short, non-chronological, high-level takeaway of the meeting. It shares no job with the timestamped timeline — chronology belongs to the timeline, recall to the summary.
_Avoid_: overview section, abstract, meeting recap timeline

**Note-product execution strategy**:
The provider-owned choice of how one note product is generated — one-shot today, chunked map/reduce later — invisible to the summarize workflow behind the per-product port call.
_Avoid_: pipeline mode, reducer flag, chunking switch
