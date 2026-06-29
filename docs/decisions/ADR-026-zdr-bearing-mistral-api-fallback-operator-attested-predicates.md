# ADR-026: ZDR-bearing Mistral API fallback: operator-attested predicates

- **Type:** Architecture
- **Status:** Superseded
- **Date:** 2026-05-01
- **Superseded by:** ADR-033
- **Related:** ADR-009, ADR-010

## Context

Local LLM may be insufficient for summarization. A remote fallback is needed but must meet strict sovereignty requirements. ZDR cannot be programmatically verified.

## Decision

ZDR-bearing direct Mistral API summarisation fallback is gated by **operator-attested contractual predicates** (commercial account posture, ZDR activation, dedicated workspace, text-only direct API use, non-Labs model posture, direct API only) plus explicit user/admin authorization. The application verifies only the narrow technical perimeter; ZDR is not programmatically verifiable. Fallback is **disabled by default**; missing operator acknowledgement yields "fallback unavailable", not best-effort remote calls. ADR-033 supersedes this fallback-specific shape with Mistral as a normal summarization provider that can be primary or fallback under accepted setup/runtime policy.

## Consequences

Refines ADR-010 and §2.3. Closes the open follow-up on legal/policy validation; residual contractual risk remains time-bounded and must be reviewed if Mistral's terms change. Resolves R-06.
