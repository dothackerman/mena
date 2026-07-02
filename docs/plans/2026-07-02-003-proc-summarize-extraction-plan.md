---
title: "Processing and Summarization Workflow Extraction - Plan"
type: feat
date: 2026-07-02
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: github-issue-118
execution: code
---

# Processing and Summarization Workflow Extraction - Plan

## Goal Capsule

| Field | Value |
|---|---|
| Objective | Complete issue #118 by extracting `process` and `summarize` orchestration out of `app/src/mena/cli.py` into dedicated workflow modules. |
| Authority | Issue #118 scope and ADRs covering runtime behavior and processing state handling. |
| Execution profile | Behavior-preserving refactor with thin CLI command adapters and stable module boundaries. |
| Stop conditions | Stop if extraction requires changing CLI-facing wording or runtime contract behavior not justified in tests. |
| Tail ownership | Continue in this branch (`develop`) and keep the branch review-ready when command slices pass tests. |

---

## Product Contract

`process` and `summarize` are split into workflow modules and invoked from `main()` as thin adapters. The CLI remains responsible for argument parsing, boundary output, and explicit runtime/resource cleanup side effects, while orchestration modules own execution sequencing, fallback policy application, and source audio preparation.

## Requirements

- R1. Move processing orchestration from `cli.py` into `app/src/mena/processing_orchestration.py`, including command execution, stage orchestration, runtime-fallback re-selection flow, and source preparation helpers.
- R2. Move summarize orchestration from `cli.py` into `app/src/mena/summarize_workflow.py`.
- R3. Move processing-state query helpers into `app/src/mena/processing.py` so shared checkpoint helpers are versioned in one place.
- R4. Keep `main()` process/summarize branches thin adapters that call workflow modules and map exceptions/output messages.
- R5. Preserve runtime fallback confirmation semantics (`--allow-runtime-fallback`, optional interactive confirmation) through callable dependencies.
- R6. Keep terminal LLM resource release logic at the CLI/workflow boundary to preserve ADR-030 intent and current printed user feedback.
- R7. Keep behavior and output stable except for test-only import/monkeypatch paths; all existing process/summarize behaviors remain covered by tests.

## Implementation Units

- **U1. Create processing orchestration module**
  - Add `app/src/mena/processing_orchestration.py` with a direct lift of `_run_processing_command`, `_run_processing_stages`, source-prep helpers, and runtime-fallback behavior.
  - Accept provider factories, stores, and boundary callbacks via parameters; keep concrete providers/defaults in `cli.py`.

- **U2. Create summarize workflow module**
  - Add `app/src/mena/summarize_workflow.py` with `_run_summarize_command` body lifted from `cli.py`.
  - Make runtime stewardship recording passed in as an injectable boundary callback.

- **U3. Move state query helpers**
  - Add `_latest_processing_state`, `_processing_state_order`, `_latest_retryable_processing_state`, `_latest_resumable_processing_state`, `_latest_non_retryable_processing_failure` to `app/src/mena/processing.py`.
  - Re-export lightweight wrappers from `cli.py` temporarily for test stability.

- **U4. CLI delegation and compatibility**
  - Replace in-command workflow bodies in `main()` with thin calls to the new modules.
  - Keep CLI-facing helper wrappers with narrow behavior and compatibility tests in `test_cli.py` while shifting logic ownership.

- **U5. Test realignment**
  - Keep existing assertions but ensure they target behavior contracts after moving ownership.
  - Keep monkeypatches stable by routing CLI private helpers to module implementations.

## Verification Contract

| Gate | Scope | Done Signal |
|---|---|---|
| Unit behavior | `app/tests/test_cli.py` and targeted new module tests | Process + summarize flows, capture segment prep, resume/fallback routing, and summarize stage execution checks remain passing. |
| Static checks | `make check` from `app/` | Static checks and tests green. |
| LFG residuals | `ce-code-review` review + final notes | Self-review applied and local validation recorded. |

## Definition of Done

- `main()` delegates `process`/`summarize` execution to new modules.
- Processing/summarize orchestration removed from CLI logic body.
- State query helpers now live in `processing.py`.
- Test/behavior coverage for moved behavior remains green.
- No behavior regressions in user-visible output text covered by existing tests.
