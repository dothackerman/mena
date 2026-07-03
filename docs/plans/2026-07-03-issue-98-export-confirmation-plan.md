---
title: "Export Command UX Confirmation and Session Discovery - Plan"
type: feat
date: 2026-07-03
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: github-issue-98
execution: code
---

# Export Command UX Confirmation and Session Discovery - Plan

## Goal Capsule

| Field | Value |
|---|---|
| Objective | Make `mena export` behave as an intentional, readable action by confirming output format and highlighting the exported Markdown file path, while preserving technical path-based behavior. |
| Authority | Issue #98 and adjacent export acceptance criteria. |
| Execution profile | Behavior-focused CLI changes with targeted test updates. |
| Stop conditions | Stop if no path-based fallback can be made safe or if `export` output still obscures the readable artifact path. |
| Tail ownership | Keep branch on `develop` and keep output-focused behavior changes isolated to export command and tests. |

---

## Requirements

- R1. Make `export` session path optional and default to the latest summarized session in the user sessions root.
- R2. Keep technical mode support when an explicit `session_path` is provided.
- R3. Present Markdown as the available export format and confirm it in interactive runs.
- R4. Show a concise output that explicitly names the readable Markdown file location.
- R5. Remove verbose provenance dump from normal successful export output.
- R6. Update CLI tests to assert new interactive/default selection behavior and concise output.

## Implementation Units

- **U1. Export parser updates**
  - Add optional positional `session_path` for `export`.
  - Add `--format` option constrained to `markdown`.

- **U2. Export session resolution**
  - Add logic to discover the latest summarized session under `settings.paths.sessions_dir` when no session path is supplied.
  - Retain explicit session-path handling and session integrity checks.

- **U3. Export command execution flow**
  - Introduce format confirmation helper for interactive runs.
  - Update output to `Markdown notes exported to: <path>`.

- **U4. Test updates**
  - Update existing export test assertions to the new success output.
  - Add default-session/format-confirmation coverage test using settings-root sessions.

## Verification Contract

| Gate | Scope | Done Signal |
|---|---|---|
| Unit behavior | `app/tests/test_cli.py` | Export command prints concise path and resolves default session when omitted. |
| Local behavior | Manual invocation under tests with `settings=AppSettings.from_env(..., home=tmp_path)` | `mena export` without session path selects latest summarized session and confirms markdown interactively. |

