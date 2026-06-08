---
status: committed
phase: commitment
hierarchy: stakeholder-requirement
babok-type: stakeholder
created: 2026-06-08
author: PM (Kai)
committed-to: "Issue #40 UAT"
traces-to: "docs/requirements/poc-requirements.md; docs/requirements/poc-model-config-local.md; docs/decisions/ADR-028-installation-profiles-local-setup-state-and-preflight-separation.md"
---

# Prepare Installed Mena for Local Processing

## Status

`[COMMITTED]`

## BABOK Classification

**Requirement type:** Stakeholder requirement
**Hierarchy level:** User-facing PoC readiness behavior
**Parent requirement:** `docs/requirements/poc-requirements.md` §4.1 precondition: "mena is installed and configured"
**Related solution slice:** `docs/requirements/poc-model-config-local.md`

## Context

The original PoC requirements state that "mena is installed and configured" before the user records and processes a meeting. During issue #40 UAT, that phrase proved too broad.

A user can have an installed `mena` command that reports a version, while local processing is still not ready. A user can also complete setup for one supported local runtime profile, while later processing accidentally uses a different provider/model/device posture if the setup-confirmed profile is not treated as the processing contract.

For UAT to be meaningful, the user-facing product contract must distinguish installed-product availability from setup readiness.

## User Goal

As a PoC user, I want to know whether the installed Mena product is actually ready to process my meeting locally, so that I can trust the result and understand what Mena will do before it records or processes meeting data.

## Scope

This requirement covers the installed-product setup journey for the Linux CLI PoC.

The PoC interface is already the CLI; this document does not reopen that decision. It refines what the user must be able to understand and trust after installing the CLI product.

## User-Visible States

### Product Not Installed

Mena is not available as an installed product command. The user cannot verify a running product version with the plain `mena` command.

This is not setup failure. Setup has not started because the product is not available.

### Installed Product

The Mena CLI product is installed and version-checkable.

The user can run a version check and see which installed product version is being used. This proves the installed product command exists. It does not prove local processing readiness.

### Setup Not Complete

Mena is installed, but local processing readiness has not been confirmed.

The user may inspect existing sessions and product information where supported, but Mena must not present recording or processing as ready until setup has been completed for a supported local runtime profile.

### Setup Ready

Mena has evaluated the local machine against a supported local runtime profile, shown the user a concrete setup readiness report, and the user has completed or accepted the setup path.

The user can reasonably expect future processing to use that accepted profile unless Mena explicitly says otherwise.

### Accepted Profile Unavailable

The user previously completed setup, but Mena can no longer use the accepted profile at processing time.

Examples include missing local services, unavailable model assets, missing credentials for gated local model preparation, changed hardware availability, removed packages, or an incompatible installed product/runtime state.

This is not a normal hidden fallback situation. It is a user-visible readiness problem.

### Explicit Fallback For This Run

Mena offers an alternate supported runtime path for the current run, and the user explicitly confirms it.

The user must understand that the output may have different quality, speed, privacy, cost, or provenance characteristics. The produced artifacts must make the fallback visible after the fact.

## PoC Setup Readiness Report

For the PoC, setup readiness should be reported as one full-workflow readiness outcome: either Mena is ready for the local meeting-processing workflow, or setup is incomplete.

The report should still show which part is missing so the user can act without understanding provider internals.

### Required PoC Readiness Categories

The setup readiness report must cover these user-visible categories:

1. **Installed product identity**
   - installed product version
   - current executable being used
   - whether plain `mena` resolves to an installed product command
   - whether the current invocation is installed-product usage or repository/development usage

2. **Supported local runtime profile**
   - accepted or evaluated profile name
   - profile version
   - profile coverage: transcription, diarization, summarization, semantic review
   - local-only or fallback posture for each stage at setup time

3. **Machine support**
   - operating system support
   - minimum local hardware support relevant to the selected profile
   - whether hardware detection is sufficient to make a supported/unsupported decision

4. **Local runtime and command readiness**
   - local LLM runtime readiness
   - Linux audio/capture runtime readiness
   - local playback readiness for correction review
   - required local command availability

5. **Provider package readiness**
   - transcription provider package readiness
   - diarization provider package readiness
   - whether the installed product includes the provider extras required by the selected profile

6. **Model and cache readiness**
   - transcription model readiness
   - diarization model readiness
   - summarization model readiness
   - semantic-review model readiness
   - local model/cache location where useful for support and UAT

7. **Credential and upstream-terms readiness**
   - whether a required local model access token is visible to Mena
   - whether missing upstream model access blocks setup
   - Mena must not claim to accept third-party terms on the user's behalf

8. **Setup actions**
   - actions Mena can perform after user confirmation
   - manual actions the user must perform outside Mena
   - actions Mena will not perform

9. **Final readiness outcome**
   - ready for local processing
   - setup incomplete
   - unsupported on this machine for the PoC profile
   - accepted profile unavailable at processing time

### Recommended PoC Action Classification

Mena may perform only narrow local setup actions after confirmation:

- pull or warm up a configured local model
- run provider model warm-up checks in the installed Python environment
- write or update Mena-owned local setup/configuration state

Mena must report these as manual actions rather than doing them itself:

- install or start system services
- install system packages
- install or start Ollama
- install or enable PipeWire/WirePlumber user-session tools
- install local playback tools
- reinstall the wheel with required provider extras
- accept upstream model terms
- create, store, or embed third-party credentials
- configure remote fallback

### Recommended PoC Blocking Categories

For the PoC, the following should block "setup ready" status:

- plain `mena` does not resolve to an installed product command
- the current invocation is repository/development usage when installed-product setup is being tested
- the machine cannot satisfy the fixed local runtime profile
- hardware support cannot be determined well enough to evaluate the profile
- required local runtime commands or services are missing or unreachable
- required provider packages are missing from the installed product environment
- required model assets cannot be found, verified, downloaded, or warmed after confirmed setup actions
- required local model access token or upstream model access is missing
- local playback is unavailable for the correction workflow
- any required manual action remains unresolved

This strict full-readiness rule is appropriate for issue #40 UAT because the goal is to test the installed product's end-to-end meeting workflow, not to explore partial capability modes.

## Post-PoC Considerations

The following are intentionally not required for the PoC, but are likely product questions after the PoC:

- partial readiness by workflow, such as "recording ready but correction playback unavailable"
- multiple local runtime profiles with user-facing selection
- automatic profile recommendation based on hardware
- administrator-managed profile policy
- profile catalog updates outside product releases
- persistent doctor/re-setup flows
- installation-level fallback preferences
- silent fallback policies for low-risk changes
- desktop-app setup UX
- richer explanation of model quality/speed tradeoffs for non-technical users

## Main Success Flow

1. The user installs the Mena CLI product.
2. The user verifies the installed product version.
3. The user asks Mena whether local processing is ready.
4. Mena shows the concrete setup readiness report for the supported local runtime profile.
5. Mena labels each missing item as either a confirmed setup action Mena can perform or a manual action the user must perform outside Mena.
6. Mena clearly says whether the final outcome is ready for local processing, setup incomplete, or unsupported for the PoC profile.
7. The user confirms the setup path or completes the required manual actions and reruns setup.
8. Mena verifies the resulting local readiness state.
9. Mena records that setup is ready for the accepted supported local runtime profile.
10. Later recording and processing use the accepted profile unless Mena explicitly interrupts the user flow.

## Alternative Flows

### Setup Declined

If the user declines setup, Mena remains installed but not setup-ready.

Mena should communicate this as an incomplete readiness state, not as a product failure.

Suggested product wording:

> Mena is installed, but local processing setup is incomplete. Complete setup before recording or processing meetings.

### Required Manual Action

If setup requires an action Mena cannot or should not perform, Mena tells the user what is missing and why setup cannot complete yet.

Examples include accepting upstream model terms, providing a local model access token, installing or starting an external local service, or installing system-level audio/runtime dependencies.

Mena must not pretend setup is complete when a required manual action remains unresolved.

### Unsupported Local Machine

If the machine cannot satisfy the supported local runtime profile, Mena tells the user that local processing is not supported by the current PoC setup.

Mena should explain the blocking category in user-meaningful terms, such as insufficient hardware, missing operating-system capability, missing local runtime, or unsupported provider readiness.

### Accepted Profile Unavailable At Processing Time

If processing is requested and the accepted profile is unavailable, Mena stops before producing new processing artifacts that would imply the accepted profile was used.

Mena tells the user what changed or what is unavailable, as far as it can determine, and offers only supported next steps.

### Fallback Available

If a supported fallback is available, Mena may offer it in the same flow.

Fallback requires explicit user confirmation for the run. For the PoC, fallback must not happen silently.

Suggested product wording:

> Your configured local processing setup is unavailable. Mena can use Basic local processing for this run if you confirm. The output will show that fallback was used.

### Fallback Declined

If the user declines fallback, Mena does not process the meeting with the fallback path.

The meeting recording and existing session artifacts remain intact.

## Product Rules

1. Installed does not mean setup-ready.
2. Version-checkable does not mean setup-ready.
3. Setup-ready means Mena has an accepted supported local runtime profile for this installation.
4. Processing must follow the accepted setup profile unless the user explicitly confirms a different supported path.
5. For the PoC, fallback is explicit per run, not silent and not implied by provider defaults.
6. Setup must not silently install dependencies, download model assets, accept third-party terms, embed credentials, or configure remote fallback.
7. Mena must not let the user believe one configured local setup was used when processing actually used another.
8. Existing recordings and session artifacts remain user data and must not be deleted because setup fails, setup is declined, or fallback is declined.

## UAT Evidence Requirements

Issue #40 UAT must be able to tell which product and setup state was tested.

UAT evidence for processing runs should include:

- installed Mena product version
- whether the run used the installed product command rather than a repository checkout
- accepted supported local runtime profile name and version
- actual runtime profile used
- transcription provider, model, and device posture
- diarization provider, model/checkpoint, and device posture
- summarization provider and model posture
- semantic-review provider and model posture where applicable
- whether setup readiness was checked before processing
- whether fallback was offered, accepted, declined, or not available

## Explicitly Out of Scope

- Reopening the PoC CLI interface decision
- Designing a desktop app setup flow
- Multiple user-selectable local profiles
- A general recommendation engine
- Telemetry-driven profile selection
- Automatic third-party account or token provisioning
- Silent fallback policy for a future non-PoC product
- Implementation details of profile schemas, provider adapters, or device handoff

## Traceability

| From | To | Relationship |
|------|----|--------------|
| `docs/requirements/poc-requirements.md` §4.1 | This requirement | Refines "installed and configured" precondition |
| This requirement | `docs/requirements/poc-model-config-local.md` | Uses committed PoC local model configuration as the concrete profile slice |
| This requirement | `docs/decisions/ADR-028-installation-profiles-local-setup-state-and-preflight-separation.md` | Depends on architecture separation of profile catalog, local state, and user choice |
| This requirement | Issue #40 | Defines UAT readiness evidence and user-facing setup behavior |

## Phase Transition Log

| Date | Phase | Notes | Author |
|------|-------|-------|--------|
| 2026-06-08 | Commitment | Extracted from issue #40 UAT discussion after issue #71 exposed that installed-product testing requires a clearer setup-readiness contract | Kai |
