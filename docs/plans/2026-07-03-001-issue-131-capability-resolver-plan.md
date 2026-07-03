---
title: Finish capability resolver ownership for issue 131
type: fix
date: 2026-07-03
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Finish capability resolver ownership for issue 131

## Goal Capsule

- **Objective:** Complete issue #131 by moving issue-scoped provider defaulting and binding for processing/summarization into the resolver path while preserving existing behavior and keeping provider validation protocol-first.
- **Authority:** GitHub issue #131.
- **Execution profile:** Minimal, behavior-preserving code cleanup around runtime resolver ownership for `cli.py`, `runtime_profile.py`, and `summarize_workflow.py`.
- **Stop conditions:** Stop if implementation requires architectural scope changes beyond #131 (new provider strategies, semantic fallback policy, or command UX changes).

## Product Contract

### Summary

Resolver-owned runtime binding is incomplete for providers and tests currently show leftover wiring from workflow defaults and direct resolver bypass for summarization.

### Requirements

- R1: `cli.py` must not expose concrete provider classes as default arguments for provider wiring.
- R2: Runtime selection/binding for summarization must route through the resolver path used by processing workflows.
- R3: `provider_validation.py` must no longer include `_provider_implements` duck-typing once protocol validation is enforced.
- R4: Keep existing behavior and existing interfaces stable where callers inject custom providers.

### Scope

In-scope:

- Update `cli.py` production command defaults and calls.
- Refactor `runtime_profile.py` to expose resolver-owned summarization provider binding.
- Refactor `summarize_workflow.py` to use that resolver binding.
- Remove obsolete duck-typing helper in `provider_validation.py` and migrate callers.
- Update/extend tests for changed call paths where needed.

Out of scope:

- Semantic CPU fallback/expanded summarization fallback behavior.
- New provider implementations.

## Implementation Units

### U1. Remove concrete provider defaults from CLI execution

- **Goal:** Keep only seam defaults that are needed for tests.
- **Files:** `app/src/mena/cli.py`, `app/tests/test_cli.py`
- **Acceptance:** `cli.py` no longer imports/uses production concrete provider classes for runtime default factories in command entry points.

### U2. Route summarization provider acquisition through resolver path

- **Goal:** Move summarization provider creation behind a resolver-owned binder.
- **Files:** `app/src/mena/runtime_profile.py`, `app/src/mena/summarize_workflow.py`, `app/tests/test_summarize_workflow.py`
- **Acceptance:** Summarization workflow binds provider through a resolver-like helper and does not directly call `runtime_summarization_provider` from `cli.py` call-site.

### U3. Drop workflow duck-typing in provider validation

- **Goal:** Remove `_provider_implements` and rely on protocol instance validation where needed.
- **Files:** `app/src/mena/provider_validation.py`, `app/tests/test_runtime_profile.py`
- **Acceptance:** `_provider_implements` is removed and tests still validate injected providers through runtime validation entry points.

## Verification

- `app` lint/type/test gate is expected to remain green.
- Behavioral assertions from issue 131 verification checklist remain true.
- `make check` from `app/` runs clean.
