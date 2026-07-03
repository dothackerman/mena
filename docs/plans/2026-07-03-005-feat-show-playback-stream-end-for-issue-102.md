---
title: Show when selected playback stream ends during recording
type: feat
date: 2026-07-03
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: https://github.com/dothackerman/mena/issues/102
execution: code
---

# Show when selected playback stream ends during recording

## Goal Capsule

| Field | Value |
|---|---|
| Objective | Emit and preserve a user-visible indication when the selected playback stream disappears during an online recording and clearly state whether other selected sources (especially the local microphone) continued capturing. |
| Authority | GitHub issue #102, `docs/architecture/architecture-overview.md`, `app/src/mena/recording_control.py`, `app/src/mena/capture_worker.py`, `app/src/mena/cli.py`, and `app/tests/`. |
| Execution profile | Small software change touching production capture-to-cli pathways and adapter tests only. |
| Stop conditions | Stop if the behavior would require additional capture-provider protocol fields or invasive changes outside route-event + notice rendering scope. |
| Tail ownership | Open a PR against `develop`; branch validation should call `make check` from `app/` and call out whether installed-product hardware UAT was executed. |

---

## Product Contract

### Summary

Issue #102 is a PM-owned observability gap: when a selected playback stream disappears before user stop, users see completion output but cannot tell that meeting-audio stopped while other sources may still have been recording.

Mena already receives capture-route events and persists them as session degradations, but current console wording is generic and does not consistently communicate what happened to the selected playback stream versus other sources.

The scope here is to improve event-to-operator messaging during live capture and at handoff, while preserving existing session semantics.

### Problem Frame

`record` already emits route events for dedicated-route fallback/degradation, and captures are persisted in the session record.

Users need confidence about whether the recording still contains useful local audio once meeting audio stops, and the final message should carry that context without changing capture behavior.

### Requirements

- R1. During capture, a route event for playback-source disruption explicitly communicates playback stream loss/end and whether local microphone capture remained active.
- R2. `record.capture_route_event` notices include enough structured context to keep user-facing formatting stable and testable.
- R3. Final recorded session handoff output includes playback-disruption context when that event occurred.
- R4. Persisted degradation facts already written to `degradations` remain unchanged in schema and continue being recorded.
- R5. Issue #102 scenarios are covered by tests in workflow/seam tests for notices and session output.

### Acceptance Examples

- AE1. Given an online recording in progress, when the selected playback stream disappears, then the CLI reports a dedicated playback-route loss event and explicitly states whether local microphone capture continued.
- AE2. Given playback loss happened before stop, then final handoff output includes an explicit note that the stream ended and what other selected source(s) were still recording.
- AE3. Given route recovery or fallback events are still sent by capture provider, existing `capture_route_event` notices continue to emit and tests asserting that signal remain green.
- AE4. Given no playback-loss event, successful finalization output remains unchanged except for added optional helpers and continues to show `mena process`.

### Scope Boundaries

- In scope: capture-route event context enrichment, route-event console rendering, session-based playback-loss handoff note, and adapter/recording-control seam tests.
- Out of scope: changing PipeWire capture algorithms, adding new protocol event types, or modifying `capture_route_event` schema definitions.
- Deferred: introducing richer structured telemetry/telemetry UI and post-processing dashboards.

## Planning Contract

### Key Technical Decisions

- KTD1. Keep event source in route-event payloads from capture unchanged; derive user-visible phrasing from existing event fields (`event`, `code`, `source_id`, `capture_mode`) plus selected-sources context carried in adapter notices.
- KTD2. Keep session persistence untouched: continue writing `CaptureRouteEvent.to_dict()` into `degradations` and only enrich notice context for formatting and final handoff wording.
- KTD3. Render the playback-loss summary in `ConsoleRecordInteraction` both at event time and in `record.process_handoff`, so users get immediate and terminal visibility.
- KTD4. Keep tests centered on `test_recording_control` (notice context) and `test_cli_adapter` (terminal copy), not broad snapshot tests of low-level capture internals.

### High-Level Design

```mermaid
flowchart TD
  CaptureWorker -->|capture_route_event| CaptureMonitor
  CaptureMonitor -->|append_session_degradation_fact| SessionRecord
  CaptureMonitor -->|record.capture_route_event notice| ConsoleRecordInteraction
  ConsoleRecordInteraction -->|playback event| clearer line + source-continuity suffix
  process_handoff -->|if recording succeeded| PlaybackLossSummary
  PlaybackLossSummary --> ConsoleRecordInteraction --> Final handoff copy
```

### Assumptions

- Route loss for playback stream is represented by `capture_route_event` with `source_id == "system_audio"` and `code == "dedicated_route_runtime_failed"` in current behavior.
- The capture session still includes a valid `capture_plan.selected_sources` for deriving microphone presence at handoff.
- A short CLI message update is acceptable for this issue scope and does not require interactive UX redesign.

### System-Wide Impact

No capture behavior changes.

Only user-visible messaging and test fixtures for adapter output change.

### Sources & Research

- Issue #102 body and acceptance criteria.
- Existing flow in `app/src/mena/recording_control.py` (`_CaptureEventMonitor`, `run_recording_control_workflow`) and `app/src/mena/cli.py` (`ConsoleRecordInteraction`).
- Existing protocol payloads and route-event structures in `app/src/mena/protocol.py` and capture events in `app/src/mena/capture.py`.

## Implementation Units

### U1. Enrich route-event notice context with route metadata

- **Goal:** Pass richer route-event context through `record.capture_route_event` notices while preserving compatibility.
- **Requirements:** R1, R2, R4
- **Files:** `app/src/mena/recording_control.py`
- **Approach:** Add route event context fields (`code`, `source_id`, `capture_mode`, and selected source list) to `WorkflowNotice.context` for `record.capture_route_event`. Keep persistence unchanged.
- **Patterns to follow:** `_CaptureEventMonitor._handle_message` existing branch for `capture_route_event`.
- **Test scenarios:** A route-event notice includes source/mode/context needed for downstream CLI formatter and no exception is thrown for legacy notices.
- **Verification:** Degradation facts still persist in `degradations` and notices continue to include `record.capture_route_event`.

### U2. Improve adapter messaging for playback-route loss and continuation state

- **Goal:** Emit clearer notice text for playback stream disruption and differentiate local-mic continuity.
- **Requirements:** R1, R2, R3
- **Dependencies:** U1
- **Files:** `app/src/mena/cli.py`
- **Approach:** Update `ConsoleRecordInteraction.notify` formatting for `record.capture_route_event`:
  - for recovered events keep recovery wording;
  - for degraded events detect playback/system source events and emit continuity-aware messaging;
  - for other events keep backward-compatible generic degraded wording with source/code detail.
- **Patterns to follow:** existing `test_console_record_interaction_reports_adapter_notices`.
- **Test scenarios:** Given a runtime playback-loss event, output contains explicit playback-loss phrasing and a phrase about local microphone continuation when selected.
- **Verification:** Adapter output remains stable for recovered events and old test notices; new paths are covered by updated assertions.

### U3. Add final-session handoff summary when playback stream ended mid-recording

- **Goal:** Ensure final recorded session output is explicitly clear when a playback stream ended and recording still contains other selected sources.
- **Requirements:** R3, R5
- **Dependencies:** U1, U2
- **Files:** `app/src/mena/cli.py`
- **Approach:** In `record.process_handoff`, read the session record and detect relevant `degradations` and `capture_plan` context; print a concise summary if playback stream was disrupted.
- **Patterns to follow:** Existing `SessionRecord` structure and helper calls around session path resolution and process handoff.
- **Test scenarios:** Given recorded session data with a playback-route degradation fact, final output includes a dedicated handoff note.
- **Verification:** Successful handoff still prints `Recording saved and ready to process`, `Next command: mena process`, and session path.

### U4. Expand adapter tests for route-event and handoff messaging

- **Goal:** Cover all new output branches and keep old behavior assertions stable.
- **Requirements:** R5
- **Dependencies:** U1, U2, U3
- **Files:** `app/tests/test_cli_adapter.py`
- **Approach:** Update existing notice-render test with the new structured notice context and add a playback-loss scenario in handoff output using a fabricated session record with a qualifying degradation fact.
- **Patterns to follow:** current `ConsoleRecordInteraction` tests and simple session fixture patterns.
- **Test scenarios:** 
  - Given a `record.capture_route_event` with playback-loss metadata, output mentions playback loss and microphone continuity.
  - Given a handoff session containing playback-loss degradation, output includes that summary line.
- **Verification:** Assertions in adapter tests describe both runtime and finalization messaging for issue #102.

## Verification Contract

| Gate | Applies to | Expected signal |
|---|---|---|
| Notice-context contract | U1 | `capture_route_event` notices include enough metadata for continuity-aware rendering and remain backward-compatible for missing keys. |
| Recording control behavior | U1-U4 | Existing route-event capture/degradation persistence remains intact. |
| Finalization messaging | U3-U4 | `record.process_handoff` output includes playback-loss summary when applicable and does not regress successful handoff copy. |
| Tests | U2-U4 | Updated adapter tests pass for new notice strings and optional summary output. |
| App gate | U1-U4 | `make check` from `app/` passes, or failures are recorded in implementation notes. |

## Definition of Done

- A playback-route disappearance is rendered with continuity-aware wording at capture time.
- Final handoff output includes an explicit playback-loss summary for affected sessions.
- Existing degraded/recovered route events still show and degrade-related session facts still append.
- Tests cover new behavior in `app/tests/test_cli_adapter.py` and preserve existing adapter notices assertions.
- The branch diff is scoped to `app/src/mena` and `app/tests`, with `docs/plans` updated for issue #102 execution.
