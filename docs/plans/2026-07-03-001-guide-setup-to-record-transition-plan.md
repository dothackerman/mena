---
title: "Guide users from completed setup to first recording"
type: feat
date: 2026-07-03
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: github-issue-94
execution: code
---

# Guide users from completed setup to first recording

## Goal Capsule

| Field | Value |
|---|---|
| Objective | Implement the final `mena setup` completion messaging so users leaving setup know the installation is ready, what to do next, and whether optional local fallback is enabled. |
| Authority | GitHub issue #94 acceptance criteria in the repository issue tracker. |
| Execution profile | Small behavior-preserving setup-workflow output update plus unit coverage in `app/tests`. |
| Stop conditions | Stop if a regression would change setup success/failure semantics or installed-product command-path validation. |
| Tail ownership | Create/update one PR for this branch only; target base branch `develop`. |

---

## Requirements

- R1. When `mena setup` reaches ready state and exits successfully, output must state that setup is complete and the product is ready to record.
- R2. When setup exits successfully, output must include the next command (`mena record`) as the recommended next action.
- R3. When local fallback was not authorized or is unavailable, output must explicitly identify this as an optional policy decision and not merge it into readiness.
- R4. Keep all existing setup and fallback behavior unchanged for check-only, declined, and error paths.
- R5. Add/adjust tests that assert ready-path output includes all new guidance and still validates existing fallback output.

## In/Out Scope

### In scope

- `app/src/mena/setup_workflow.py` ready-to-record completion messaging.
- `app/tests/test_cli.py` (ready-path CLI output assertions).
- `app/tests/test_setup_workflow.py` (workflow event/message assertions).

### Out of scope

- Changes to setup detection, action execution, runtime profile persistence format, readiness checks, or command execution logic.
- Any behavior changes on non-ready paths.

## Acceptance Examples

- AE1. Given setup becomes ready, when it exits, then output includes “Mena is ready to record” and `mena record` before exit.
- AE2. Given setup completes without `--authorize-local-fallback`, when output is produced, then it includes explicit optional-policy text stating local fallback remains unauthorized or disabled.
- AE3. Given setup completes with `--authorize-local-fallback` and fallback is authorized, when output is produced, then it keeps the existing authorization notice and includes ready-to-record guidance.
- AE4. Given setup is not ready, when it exits, then existing “Setup declined…” and “Local provider readiness: incomplete” behavior remains unchanged.

## Planning Contract

- KTD1. Emit ready-to-record messaging as dedicated `SetupMessageEvent`s in the workflow layer so the existing report formatter remains stable.
- KTD2. Preserve existing `format_setup_report` output to avoid broad snapshot churn in existing tests; append only targeted messages.

## Implementation Units

### U1. Add ready-state handoff messages

- **Goal:** Add explicit final guidance in `run_setup_workflow` after persistence.
- **Files:** `app/src/mena/setup_workflow.py`
- **Approach:** Append `SetupMessageEvent` lines for:
  - “Setup is complete. Mena is ready to record.”
  - “Next command: `mena record`”
  - “Optional local fallback policy: authorized/enabled | not configured | disabled | unavailable” (derived from `RuntimeProfileState.fallback_authorization.local_status`).
- **Verification:** Workflow tests validate message content through `_messages(result.events)` where ready-state transitions happen.

### U2. Test lock-down

- **Goal:** Assert the new behavior and avoid regressions.
- **Files:** `app/tests/test_setup_workflow.py`, `app/tests/test_cli.py`
- **Approach:** Add/adjust assertions for:
  - completion guidance presence in successful CLI runs,
  - ready-state events for configured unauthorized and authorized fallback scenarios.
- **Verification:** Existing readiness and fallback assertions remain in place and updated tests confirm completion guidance.

## Verification Contract

| Gate | Scope | Done Signal |
|---|---|---|
| Unit tests | `app/tests/test_setup_workflow.py`, `app/tests/test_cli.py` | Successful ready path includes completion guidance and optional fallback status while preserving existing fallback notices. |
| Regression coverage | Existing ready/incomplete setup tests | No behavior changes for check-only, declined, interrupted, manual-only, or not-installed-product paths. |

## Definition of Done

- Setup completion path prints a readiness-to-record closure and recommended `mena record` command.
- Optional fallback status is explicitly expressed as policy state on completion.
- Existing fallback-authorized/unauthorized/disabled strings remain asserted in tests.
- All updated tests pass.
