# R-06 Fallback Provider Policy

Status: Ready

## Purpose

Determine whether and under what exact conditions direct Mistral API use is acceptable as the PoC summarization fallback.

This task is policy-sensitive and must produce a concrete go/no-go recommendation rather than vague reassurance.

## Product Context

`mena` is a privacy-first local meeting notes product.

The current architecture keeps:

- audio recording local-only
- transcription local-only
- diarization local-only

Summarization is local-preferred.
The only currently architecture-allowed remote fallback candidate is direct Mistral API access, and only for summarization.

The reason this task exists is not to pick the best general cloud model provider.
It exists to determine whether the current fallback idea is actually policy-compatible with the product's sovereignty and privacy posture.

## Fixed Constraints

Treat the following as fixed:

- no remote audio processing
- default-retention remote access is not architecture-compliant for product fallback
- brokered access paths such as OpenRouter are not equivalent to direct Mistral API access and are out of current scope
- if fallback is allowed at all, it must be tightly constrained and explicit
- the product must not silently imply stronger guarantees than it can really verify

## Current Architecture Baseline

Relevant baseline assumptions:

- fallback is for summarization only
- current candidate is direct Mistral API only
- a Zero Data Retention path is currently treated as the minimum acceptable policy baseline
- legal/policy suitability is not yet confirmed
- architecture needs to know what the application can verify automatically versus what must remain contractual, documented, or user-attested

## Current Facts To Validate

As of April 30, 2026, the current repo baseline records the following external claims as needing validation rather than blind trust:

- API data is not used for model training
- API input and output may be retained for abuse monitoring unless Zero Data Retention is activated
- Zero Data Retention may require support approval or contract/account conditions

These are starting points, not settled truth.
Verify current official terms and operational reality.

## Research Question

What account, contract, retention, routing, and operational conditions make direct Mistral API acceptable as the PoC summarization fallback, and what can the application realistically verify versus only document or require by attestation?

## Decision To Unblock

This research should unblock:

- whether PoC fallback may ship at all
- the exact allow/deny conditions for direct Mistral fallback
- the product behavior when policy status is unknown or unverifiable

## Evaluation Criteria

Evaluate primarily in this order:

1. real retention behavior and ZDR conditions
2. clarity and enforceability of the provider's guarantees
3. jurisdiction and routing implications relevant to the product posture
4. whether the application can verify anything programmatically or must rely on account-level state and user attestation
5. operational practicality for testers on weaker hardware
6. risk of user misunderstanding or accidental policy drift

## Required Questions To Answer

Answer at least the following:

- is direct Mistral fallback a go or a no-go for the current PoC?
- if it is a go, under exactly what conditions?
- what account, contract, workspace, or support configuration is required?
- what can the application verify technically?
- what cannot be verified and therefore must be treated as documentation, operator setup, or explicit user attestation?
- what should the product do when credentials exist but policy status is unknown?
- should the architecture allow any explicit out-of-architecture personal experimentation path, or prohibit it entirely?

## Out Of Scope

Do not spend time on:

- general provider shopping beyond what is needed for this exact decision
- brokered access paths
- implementation of fallback code

## Expected Output

The final research note should include:

- a clear go/no-go recommendation
- exact allow/deny conditions
- application-verifiable versus non-verifiable controls
- recommended product behavior for unknown or non-compliant states
- documentation requirements and residual risks

## References

Primary references:

- `docs/architecture/architecture-overview.md`
- current Mistral policy, privacy, and security documentation
