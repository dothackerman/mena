# UAT Scenarios

Status: reusable installed-product UAT scenario catalog

These scenarios define what Mena user-acceptance testing covers. They are not raw
test evidence. Release-specific runs record their verdicts under `runs/` and link
to tester workspace evidence when a scenario does not pass.

## Global Rules

- Test the installed release artifact with plain `mena`, not a development
  checkout.
- Use controlled, non-private audio only.
- Keep raw audio, transcripts, session bundles, logs, screenshots, and graph dumps
  outside the repository.
- Record enough evidence to support the verdict without copying private or bulky
  runtime data into GitHub.
- A failed or blocked scenario should link to a slim bug issue with observed
  result, expected result, release, and evidence pointers.

## Scenario UAT-A1: Release Artifact Identity

Purpose: prove the UAT run is testing the intended installed release artifact.

Pass criteria:

- wheel or release artifact identity is unambiguous
- checksum, release tag, commit, and `mena --version` are recorded
- plain `mena` resolves to the installed product

## Scenario UAT-A2: Setup Readiness

Purpose: prove the installed product is ready for the accepted local runtime
workflow before recording or processing tests continue.

Pass criteria:

- setup readiness is complete for the local PoC workflow
- accepted runtime profile and fallback authorization state are inspectable
- missing tools, credentials, or model assets produce actionable readiness output

## Scenario UAT-B1: Live Environment Readiness

Purpose: prove the runner can inspect the live Linux audio graph before Mena
mutates it.

Pass criteria:

- PipeWire/Pulse inspection works from the execution environment
- candidate microphone, output sink, and playback stream routes are visible
- no Mena-owned temporary audio objects exist before the run

## Scenario UAT-B2: In-Person Live Mic Smoke

Purpose: prove `in_person_single_mic` records selected room microphone audio.

Pass criteria:

- recording finalizes as a valid session
- pre-capture CLI output includes the recording consent notice
- sealed `room_mic` segments are present, ordered, and plausible
- record-time language and route choices are persisted
- cleanup leaves no Mena-owned capture process or unexpected audio graph state

## Scenario UAT-B3: Online Dedicated Route Smoke

Purpose: prove `online_dual_stream` captures selected local microphone input plus
selected playback/system audio while playback remains audible.

Pass criteria:

- recording finalizes as a valid online session
- pre-capture CLI output includes the recording consent notice
- `local_mic` and `system_audio` sealed segment indexes match exactly
- same-index source hashes differ when controlled sources are intentionally
  different
- sealed WAV chunks are independently readable, frame counts are contiguous, and
  local/system offsets stay stable across segment boundaries
- graph snapshots show playback moved during capture and restored after stop
- during-capture evidence shows the selected playback remained audible through
  loopback to the user's selected output sink
- cleanup leaves no Mena-owned route object or capture process

## Scenario UAT-B4: Runtime System-Audio Degradation

Purpose: prove a runtime dedicated-route capture failure degrades only to a
supported selected-output-monitor path and records that degradation.

Prerequisite: UAT-B3 passed in the same environment with the same route shape.

Pass criteria:

- typed degradation event is persisted
- at least one paired primary dedicated-route segment was sealed before the
  controlled runtime failure
- subsequent paired segments transitioned to the degraded selected-output-monitor
  path
- degraded `system_audio` segments record the effective degraded capture mode
- online source index sets still match
- Mena does not attempt unsafe recovery back to the dedicated route
- cleanup passes

## Scenario UAT-C1: In-Person Known-Fact Processing

Purpose: test processing and output quality against a controlled
`room_mic` fixture with known expected facts and non-facts.

Pass criteria:

- transcript, diarization, merged transcript, review/correction, summary, action
  items, timeline, and export artifacts are inspectable
- processing provenance records accepted profile, actual profile,
  provider/model/device posture, fallback status, and whether fallback was used
- important expected facts are present
- expected non-facts are absent
- correction/review behavior is visible when flags exist
- corrections are stored as a separate overlay while raw ASR and merged
  transcript artifacts remain unchanged
- corrected or confirmed speaker labels persist speaker-anchor artifacts with
  embedding references, or the session explicitly records that embeddings were
  unavailable

## Scenario UAT-C2: Online Known-Fact Processing

Purpose: test processing and output quality against controlled `local_mic` plus
`system_audio` fixtures.

Pass criteria:

- online source handling remains channel-aware
- processing provenance records accepted profile, actual profile,
  provider/model/device posture, fallback status, and whether fallback was used
- local microphone and system audio facts are not collapsed or swapped
- transcript, timeline, summary, and action items are judged against expected
  facts and non-facts
- correction/review behavior is visible when flags exist
- corrections are stored as a separate overlay while raw ASR and merged
  transcript artifacts remain unchanged

## Scenario UAT-D1: In-Person End-To-End Baseline

Purpose: prove the installed product can run the in-person user journey from
recording to exported notes.

Pass criteria:

- live in-person recording produces processable segments
- if this is the run's manual-notes baseline, summarization uses a sealed
  versioned manual-notes artifact and preserves the original notes unmodified
- processing, correction, summarization, and export complete
- artifacts preserve provenance and are useful enough for human review

## Scenario UAT-D2: Online End-To-End Baseline

Purpose: prove the installed product can run the online user journey from dual
stream recording to exported notes.

Pass criteria:

- live online recording produces processable paired segments
- if this is the run's manual-notes baseline, summarization uses a sealed
  versioned manual-notes artifact and preserves the original notes unmodified
- processing, correction, summarization, and export complete
- local/system source separation remains inspectable through processing output

## Scenario UAT-D3: Runtime Fallback After Accepted Profile Failure

Purpose: prove that when the accepted local runtime profile becomes unavailable
during processing, Mena either uses an explicitly authorized supported fallback
for that run or stops with an explicit fallback-unavailable result.

Requirement refs:

- `docs/requirements/poc-requirements.md` REQ-P01
- `docs/requirements/poc-installed-product-setup-use-case.md`
- `docs/decisions/ADR-029-accepted-runtime-profile-state-revalidation-and-provenance.md`
- `docs/decisions/ADR-031-runtime-fallback-authorization-and-setup-state.md`

Pass criteria:

- the accepted profile failure is visible to the user
- fallback is not silent or invented
- if automatic local fallback was explicitly authorized and is supported for the
  failed stage, the run records fallback as used and continues on that fallback
  profile
- if fallback is unavailable for the failure class, the run stops with an
  explicit fallback-unavailable result
- provenance records accepted profile, actual profile, fallback status, and
  whether fallback was used

## Scenario UAT-E1: Failure And Resume

Purpose: prove recording interruptions preserve sealed data and processing
failures resume from the failed checkpoint.

Pass criteria:

- already sealed segments remain present after interruption
- final session state is explicit
- after a simulated processing failure, processing resumes from the failed
  checkpoint without reprocessing already completed segments
- resumed processing completes successfully
- retry behavior does not destroy prior user data

## Scenario UAT-F1: Stress And Duration

Purpose: check meeting-length behavior after smoke and baseline scenarios pass.

Pass criteria:

- the stress run records for at least 60 minutes
- the stress run completes without missing or duplicated source indexes
- finalization and cleanup pass
- recorded output remains processable
- a shorter run cannot be recorded as a clean stress pass
- a 90-minute run is required to claim full REQ-P02 coverage; otherwise the
  untested 90-minute boundary remains explicit residual risk
