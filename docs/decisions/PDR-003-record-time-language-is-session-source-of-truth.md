# PDR-003: Record-time language is the session source of truth

- **Type:** Product
- **Status:** Accepted
- **Date:** 2026-06-16
- **Superseded by:** —
- **Related:** #83, REQ-F03, ADR-021, ADR-022

## Context

The PoC requirements already say the user specifies the meeting language when starting a recording. That language choice affects transcription quality because ASR configuration, vocabulary hints, and dialectal expectations are language-sensitive.

During issue #83 discussion, we found that the current CLI/product shape risks splitting language across commands: `record` should collect meeting setup facts, `process` should consume recorded session facts, and `summarize` currently has language configuration of its own. If language is only supplied later, or only to one command, Mena can produce derived artifacts that do not reflect the user's recording-time intent.

The product question is not which exact CLI flag implements language selection. The product question is where the user's selected meeting language becomes authoritative for a meeting session.

## Decision

The meeting language selected at recording start is the session-level source of truth for language-sensitive downstream processing.

- `record` is responsible for collecting or confirming the meeting language as part of the recording setup flow.
- The selected language is persisted with the session.
- `process` consumes the session language for transcription/ASR configuration and lexicon or prompt-biasing.
- `summarize` consumes the session language for summary prompt/output policy unless the user explicitly creates a new summarization run with a different language.
- Diarization treats language as session metadata unless a concrete provider requires language-specific behavior.
- Semantic coherence remains language-agnostic as a product mechanism; the session language may still be available as metadata/context, but the mechanism must not depend on language-specific dialect detection.

Configuration may provide defaults, but it must not silently override the persisted session language for an existing recording.

## Consequences

**Positive:**
- Users choose meeting language once, at the moment they understand the meeting context.
- Downstream stages can be traced back to the same session language, improving UAT evidence and reducing silent provider drift.
- Summaries are less likely to ignore the language context used for transcription.
- Config defaults remain useful for convenience without becoming hidden per-session behavior.

**Negative:**
- The session record needs to persist and expose language as product metadata.
- Existing command behavior that accepts language later must reconcile explicit rerun overrides with the recorded session language.
- Implementation must define how a user intentionally regenerates a summary in a different language without corrupting the original session facts.

**Out of scope (Architect):**
- Exact session-record schema fields.
- Exact CLI flags or prompt mechanics.
- Provider-specific parameter mapping.
- Configuration precedence mechanics beyond the product rule that session language is authoritative for the recorded meeting unless the user explicitly requests a new derived artifact in another language.
