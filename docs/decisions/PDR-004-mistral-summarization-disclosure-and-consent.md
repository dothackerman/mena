# PDR-004: Mistral summarization disclosure and consent

- **Type:** Product
- **Status:** Accepted
- **Date:** 2026-06-29
- **Superseded by:** —
- **Related:** ADR-026, ADR-033

## Context

`ADR-033` allows direct Mistral as a summarization provider under accepted setup/runtime policy. Unlike local providers, this sends meeting-derived text to the user's configured Mistral account: the transcript text selected for summarization, plus optional manual notes when the user includes them. Mena can enforce a narrow technical perimeter, but it cannot verify the user's Mistral plan, ZDR activation, legal terms, subprocessor posture, account funds, or whether a meeting's content is appropriate for that account.

This is a product disclosure and consent problem, not a separate provider architecture.

## Decision

Setup and runtime disclosure for direct Mistral summarization must be direct and specific.

Before a user selects or authorizes Mistral summarization, Mena must disclose that:

- transcript text selected for summarization, plus optional manual notes when included, is sent to the user's configured Mistral account;
- audio, ASR processing, diarization processing, local transcript artifacts, speaker embeddings, and speaker-sensitive artifacts remain local-only;
- Mistral use may incur provider charges and depends on the user's account, quota, subscription, workspace, ZDR posture, and legal terms;
- ZDR does not remove all residuals: Mistral may still retain non-content account, support, billing, abuse-prevention, or operational metadata, and Mistral-controlled routing or terms may still affect jurisdictional risk;
- Mena cannot verify ZDR approval, plan tier, training opt-out, subprocessor posture, legal isolation, endpoint routing, account funds, or whether the account is appropriate for the meeting content;
- users should not use remote summarization for meetings likely to contain special-category or similarly sensitive personal data unless they have confirmed that their Mistral account, legal basis, and contractual posture are appropriate for that content;
- provider use is recorded in provenance when Mistral is used.

Local fallback authorization never authorizes remote Mistral summarization. Mistral summarization requires its own accepted setup/runtime authorization according to `ADR-033`.

## Consequences

**Positive:**
- Users see the privacy, account, cost, and sensitive-content implications before choosing Mistral.
- The product does not overclaim what it can verify about the user's Mistral account.
- Special-category-content risk is handled where users actually make the remote-use choice.

**Negative:**
- Setup and fallback prompts need explicit copy and tests.
- The disclosure is intentionally conservative and may discourage remote summarization for sensitive meetings.

**Out of scope (Architect):**
- Exact CLI wording.
- Exact persistence fields for acknowledgement state.
- Legal review of a user's Mistral account or meeting-specific lawful basis.
