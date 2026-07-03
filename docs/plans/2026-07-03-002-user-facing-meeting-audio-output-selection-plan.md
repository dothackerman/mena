---
title: "Meeting audio output wording - Plan"
type: fix
date: 2026-07-03
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: github-issue-101
execution: code
origin_issue: 101
---

# Meeting audio output wording - Plan

## Goal Capsule

- **Objective:** Rephrase the online recording output-route selection prompt so users can choose where they hear the meeting in plain terms, while preserving the existing route-selection contract and diagnostic identifiers as secondary context.
- **Authority hierarchy:** GitHub issue #101 and `CONTEXT.md` interaction-port boundary expectations in `docs/architecture/architecture-overview.md` sections on CLI/seams.
- **Execution profile:** Small behavior-preserving UX wording adjustment with adapter-level tests and regression coverage in `app/tests`.
- **Stop conditions:** Stop before shipping if any change would alter the selected route identifier semantics, route discovery data, or non-interactive CLI flags.
- **Tail ownership:** The implementing agent owns code changes, local verification, self-review, PR updates, and residual documentation if review findings remain.

## Product Contract

### Requirements

- **R1.** The CLI selection prompt for the online meeting output destination must not expose `"sink"` as the primary verb for the user decision.
- **R2.** The prompt must describe the decision in user terms (where the meeting audio is heard: speakers/headphones/system audio output), with backend identifiers remaining secondary.
- **R3.** Route selection behavior and backend IDs must remain unchanged when selecting defaults, one-option shortcuts, and multi-option disambiguation.
- **R4.** Existing behavior for non-output routing prompts and diagnostics must remain unchanged.
- **R5.** Existing tests covering route resolution and interaction topics must continue to pass, with updated assertions for the new wording where needed.

### Scope boundaries

- In-scope: user-facing wording used by interactive route selection prompts in the recording workflow adapter and related tests.
- Out-of-scope: audio discovery logic, audio routing policy, underlying route IDs, and new command-line flags.

### Dependencies

- `app/src/mena/cli.py` — route prompt metadata and rendering entry points.
- `app/src/mena/recording_control.py` — route topic mapping retained to avoid workflow changes.
- Existing interaction port and scripted interaction tests under `app/tests`.

## Planning Contract

- **KTD-1 (Terminology split):** Keep domain labels (`output sink`, route IDs, `--output-sink`) for internal contracts but present user-facing prompt copy with a meeting-audio destination label.
- **KTD-2 (Backward compatibility):** Preserve topic names and IDs so scripted workflows and external integrations stay stable.
- **KTD-3 (Context layering):** Preserve identifier visibility in option details/labels so technical troubleshooting still has route IDs available.
- **Approach:** Change only adapter-facing title text for the `record.capture_route.output_sink` choice topic in `app/src/mena/cli.py`; keep selection and validation logic unchanged in `app/src/mena/recording_control.py`. Add/adjust tests that assert the new prompt text is surfaced while leaving existing topic-level behavior unchanged.

## Implementation Units

- **U1.** Reword output-route prompt metadata for output sink selection.
  - **Goal:** Replace `Output sink routes` as user-facing title with a meeting-audio phrase.
  - **Requirements:** Satisfy R1, R2.
  - **Files:** `app/src/mena/cli.py`
  - **Approach:** Update `_record_choice_prompt_metadata()` mapping for `record.capture_route.output_sink`.
  - **Test scenarios:** 
    - The output-route selection request still uses topic `record.capture_route.output_sink`.
    - The rendered prompt title uses meeting-audio wording in CLI output.
    - Single default route, multiple route selection, and no-route error paths remain unchanged.
  - **Verification:** Run interactive route tests (targeted) and inspect diff.

- **U2.** Add/update tests to guard the wording contract.
  - **Goal:** Ensure the new user-facing wording is covered in adapter tests while preserving interaction topic contracts.
  - **Requirements:** Satisfy R3, R5.
  - **Files:** `app/tests/test_recording_control.py`
  - **Approach:** Extend existing scripted interaction expectations for route prompts if they assert request shape or rendered text.
  - **Test scenarios:** 
    - Ambiguous route selection sequence still emits `record.capture_route.output_sink`.
    - Output-route prompt string change is explicitly asserted.
  - **Verification:** Run relevant test modules for recording control interaction behavior.

## Verification Contract

- Run focused test command(s):
  - `cd app && uv run pytest tests/test_recording_control.py`
  - `cd app && uv run pytest tests/test_cli.py -k capture_route`
- If command-level verification is unavailable in local mode, run `cd app && uv run pytest tests/test_recording_control.py`.
- No new tests are needed for routing internals in `recording_control.py`; regression coverage is at adapter interaction boundaries.

## Definition of Done

- **Global:** Plan requirements are implemented without changing route selection semantics.
- **U1 complete:** Users see meeting-audio wording for the output-route prompt, and no `"Output sink routes"` user-facing title remains.
- **U2 complete:** Focused tests pass with updated wording assertions where applicable.
- **Cleanup:** No schema or CLI contract changes; no debug-only prints or prompt text duplication.
- **Shipping readiness:** Working tree clean after commit, with behavior-preserving diff limited to user-facing text and tests.
