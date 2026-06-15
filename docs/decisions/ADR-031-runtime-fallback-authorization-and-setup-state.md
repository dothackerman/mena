# ADR-031: Runtime fallback authorization and setup state

- **Type:** Architecture
- **Status:** Accepted
- **Date:** 2026-06-15
- **Superseded by:** -
- **Related:** #78, ADR-026, ADR-028, ADR-029, ADR-030

## Context

`ADR-029` made runtime fallback explicit per run. Issue #78 adds a stricter product requirement: post-meeting processing should prefer checkpoint-safe completion over speed, and local CPU fallback should be able to run without repeated user interruption when the user has already authorized that behavior.

At the same time, runtime code must not become a profile recommender. Choosing which primary and fallback profiles are acceptable belongs to setup, where Mena can present hardware evidence, policy constraints, expected tradeoffs, and user/admin choice.

Local CPU fallback and future remote fallback are different decisions. Local fallback affects speed, resource use, and possibly model quality. Remote fallback may move meeting substance off-machine and is governed by sovereignty policy.

## Decision

Setup persists both runtime profile selection and fallback authorization as product state.

The accepted setup state must distinguish:

- accepted primary runtime profile
- accepted fallback profile or fallback capabilities
- whether automatic local fallback is authorized
- the scope of that authorization
- whether fallback changes only placement, or may also change model/checkpoint/profile
- typed remote fallback authorization, if any, separately from local fallback authorization

Runtime commands consume accepted setup state. They must not invent new fallback profiles during processing.

When local fallback is needed:

- if automatic local fallback was authorized at setup, Mena may use the accepted local fallback profile without prompting again
- if automatic local fallback was not authorized and the command is interactive, Mena asks before using fallback
- if automatic local fallback was not authorized and the command is non-interactive, Mena fails unless the command carries an explicit one-run fallback authorization
- setup can be rerun to change the fallback authorization decision

Fallback remains primary-first:

- the accepted primary profile is preferred whenever current evidence says it is schedulable
- a prior fallback use does not permanently demote the run or future work items
- Mena may avoid a primary retry when current resource evidence shows that retry would predictably fail

Remote fallback authorization is a separate typed decision:

- local fallback authorization never authorizes remote fallback
- remote summarization fallback remains governed by `ADR-026`
- future remote fallback profiles must record their own policy evidence, consent/authorization state, and stage scope

The CLI may continue to expose an explicit one-run fallback flag, but that flag is an adapter-level way to satisfy the same authorization contract. It does not replace persisted setup authorization.

## Consequences

Positive:

- Users can choose whether Mena should automatically use slower local fallback to complete post-meeting processing.
- Runtime behavior remains predictable because processing only uses accepted profiles and accepted authorization.
- Future remote fallback can be added without confusing local CPU fallback consent with sovereignty-sensitive consent.
- Non-interactive processing has a clear failure contract when fallback was not pre-authorized.

Tradeoffs:

- Setup state must evolve beyond a single accepted profile into accepted primary plus fallback authorization metadata.
- Setup copy must clearly explain speed, quality, and privacy implications of each fallback type.
- Tests must cover setup authorization, interactive fallback prompting, non-interactive refusal, and provenance.

Deferred:

- Product UX wording for desktop setup.
- Remote fallback authorization beyond the already constrained summarization path.
- Telemetry-informed fallback recommendations.
