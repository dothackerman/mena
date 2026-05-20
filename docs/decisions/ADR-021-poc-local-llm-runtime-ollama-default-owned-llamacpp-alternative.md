# ADR-021: PoC local-LLM runtime: Ollama default, owned llama.cpp alternative

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-05-01
- **Superseded by:** —
- **Related:** ADR-010, ADR-022

## Context

Local LLM inference is needed for summarization and semantic flagging. Ollama provides the easiest default experience; an owned llama.cpp adapter validates the Inference Port boundary. vLLM adds complexity without proportional PoC benefit.

## Decision

PoC default local-LLM runtime adapter is **Ollama**, with owned `llama.cpp` (`llama-server`) as the first quality-validated alternative once model-dependent workflow code exists; vLLM is out of PoC scope. Default model profile: 3B–4B-parameter quantised instruct; 1B–3B fallback on ≤8 GB RAM machines; 7B opt-in for stronger machines; no 12B-class baseline.

## Consequences

Workflow code asks for stage-level capabilities, not for `/v1/chat/completions`. Replaceability is preserved by validating the Inference Port with an owned `llama.cpp` adapter before model work becomes product-critical. Resolves R-01.
