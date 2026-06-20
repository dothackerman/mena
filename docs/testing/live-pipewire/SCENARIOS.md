# Live PipeWire Test Scenarios

Status: manual live-environment scenarios for Linux PoC validation

These scenarios verify behavior that unit tests and fake PipeWire clients cannot prove:

- `pw-record` captures real selected sources over time.
- `pactl move-sink-input` works against a real PipeWire/Pulse graph.
- the dedicated online route keeps playback audible through loopback.
- normal stop and failure paths restore the audio graph.
- sealed source segments are ordered, aligned, and processable.
- degraded online capture stays degraded until stop.

## Global Rules

- Use only controlled, non-private audio.
- Do not write audio recordings or raw session bundles into the repository.
- Do not record real meetings, calls, notifications, or private speech.
- Do not mention played content in committed docs, protocol rows, evidence summaries, or PR comments.
- Use a scratch data root such as `/tmp/mena-live-smoke-data/<run-id>/`.
- Store run evidence outside the repository, for example under the operator workspace.
- Before, during, and after every mutating live UAT run, capture sanitized PipeWire/Pulse graph snapshots.
- A during snapshot means after Mena reports capture has started and before stop/finalization.
- Graph evidence must prove the selected playback stream, Mena temporary route objects, and cleanup state without recording content details.
- A scenario that leaves Mena-owned sinks, loopbacks, capture workers, or `pw-record` processes behind fails cleanup.

## Scenario S1: Environment Readiness

Purpose: prove the test runner can inspect the live PipeWire/Pulse graph before any Mena mutation.

Actions:

- record tool availability for `pactl`, `pw-dump`, and `pw-record`
- capture read-only snapshots of sinks, sources, sink inputs, modules, and PipeWire graph state
- identify candidate microphone, output sink, and playback stream routes
- confirm the scratch data root and evidence folder are empty or run-scoped

Pass criteria:

- PipeWire/Pulse inspection works from the execution environment
- candidate routes are visible enough for explicit Mena CLI selection
- no Mena-owned temporary audio objects exist before the run

Stop condition:

- if the execution environment cannot reach PipeWire/Pulse, do not proceed to mutating scenarios from that environment

## Scenario S2: In-Person Mic Smoke

Purpose: prove selected-microphone capture works with real room audio.

Actions:

- start controlled room audio
- run `mena record` with the in-person profile, explicit language, explicit microphone route, and scratch data root
- allow enough time for multiple source segments
- stop recording through the normal CLI path
- inspect session metadata and source segment inventory

Pass criteria:

- session finalizes as a valid recording when segments were sealed
- sealed `room_mic` segments are present and ordered
- segment durations and byte counts are plausible for the configured format
- no Mena-owned capture process remains after stop
- no unexpected audio graph changes remain after stop

## Scenario S3: Online Dedicated Route Smoke

Purpose: prove the selected online playback stream can be isolated through the dedicated route while local microphone capture continues.

Actions:

- start controlled local microphone input and controlled playback output
- identify the active playback stream and selected output sink
- run `mena record` with the online profile, explicit language, explicit microphone route, explicit playback stream, explicit output sink, and scratch data root
- allow enough time for multiple paired source segments
- stop recording through the normal CLI path
- inspect session metadata and paired segment inventory

Pass criteria:

- session finalizes as a valid online recording when paired segments were sealed
- `local_mic` and `system_audio` sealed segment indexes match exactly
- `local_mic` and `system_audio` same-index hashes are not identical when controlled sources are intentionally distinct
- paired source segment durations and byte counts are plausible
- segment metadata records the effective online capture route
- before/during/after snapshots show the selected playback stream moved during capture and restored after stop
- playback is restored to the selected output sink
- no Mena-owned null sink, loopback module, capture worker, or `pw-record` process remains after stop

## Scenario S4: Runtime System-Audio Degradation

Purpose: prove a runtime dedicated-route capture failure degrades to selected-output-monitor capture without attempting unsafe recovery.

Prerequisite: S3 passes in the same environment.

Actions:

- start an online recording as in S3
- after at least one paired segment is sealed, inject a controlled failure into the system-audio capture path
- continue recording long enough to seal degraded paired segments
- stop recording through the normal CLI path
- inspect route events, degradation metadata, segment inventory, and cleanup evidence

Pass criteria:

- Mena records a typed degradation event
- recording continues after the failure when the selected output monitor is available
- degraded `system_audio` segments are marked with the effective degraded capture mode
- `local_mic` and `system_audio` sealed segment indexes still match exactly
- Mena does not attempt runtime recovery back to the dedicated route
- playback and temporary route objects are cleaned up after stop

Stop condition:

- if the failure injection affects unrelated user audio, stop the scenario and perform cleanup verification

## Scenario S5: Setup-Time Dedicated Route Failure

Purpose: prove setup failure after partial route mutation restores playback before surfacing failure or degradation.

Prerequisite: S3 route discovery succeeds.

Actions:

- start controlled playback
- trigger a controlled setup-time failure after Mena has enough route context to exercise cleanup
- inspect final CLI result, session state if one was created, and audio graph snapshots

Pass criteria:

- playback is restored to the selected output sink
- no Mena-owned route modules remain
- no capture worker or `pw-record` process remains
- outcome is explicit: failed setup, cancelled setup, or degraded start depending on the injected failure point

## Scenario S6: Short Online Soak

Purpose: check basic stability over a longer but bounded online run.

Prerequisite: S3 passes.

Actions:

- run the online profile with controlled local and playback audio for 10 to 15 minutes
- stop through the normal CLI path
- inspect all sealed segment indexes, durations, route metadata, and cleanup evidence

Pass criteria:

- no missing or duplicated sealed segment indexes
- online source index sets match exactly
- no frame-count or duration anomalies suggest boundary drift
- cleanup passes

## Scenario S7: Long Online Soak

Purpose: approximate a meeting-length recording after shorter smoke and soak scenarios pass.

Prerequisite: S6 passes.

Actions:

- run the online profile with controlled local and playback audio for the target meeting-length window
- stop through the normal CLI path
- inspect segment inventory, route events, finalization metadata, and cleanup evidence

Pass criteria:

- all sealed online segment index sets match exactly
- finalization is valid and processable
- cleanup passes
- no evidence artifact contains played-content details

## Evidence Expectations

For each run, preserve:

- command log
- before/during/after PipeWire/Pulse snapshots
- session summary without transcripts or audio content
- segment inventory with source ids, indexes, statuses, durations, sizes, checksums, and relative paths
- protocol results CSV
- operator notes that describe behavior without describing played content
