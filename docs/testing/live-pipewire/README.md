# Live PipeWire Test Protocol

Status: manual live-environment protocol for Linux PoC validation

This folder defines reusable live PipeWire smoke and soak tests for `mena record`.
It intentionally stores only scenario definitions and protocol structure.

## Privacy Boundary

Committed files in this folder must not contain:

- recorded audio
- transcripts or summaries
- phrases, lyrics, titles, meeting names, or other playable-content details
- raw Mena session bundles
- machine-specific route snapshots from a test run

Runtime audio and session data must be written outside the repository, for example:

```text
/tmp/mena-live-smoke-data/<run-id>/
```

Run evidence that is useful for review but not part of the product repository should be written to the operator's evidence workspace, for example:

```text
/home/oriol/.openclaw/workspace/architect/artifacts/mena-live-pipewire-smoke/runs/<run-id>/
```

Evidence may include logs, PipeWire/Pulse snapshots, segment inventories, hashes, and notes, but should still avoid human-readable audio content.

## Run Evidence Layout

Create a run-scoped data root and evidence directory before each live UAT run:

```sh
RUN_ID="$(date -u +%Y%m%d-%H%M%S)-kai-uat"
DATA_ROOT="/tmp/mena-live-smoke-data/$RUN_ID"
EVIDENCE_DIR="/home/oriol/.openclaw/workspace/architect/artifacts/mena-live-pipewire-smoke/runs/$RUN_ID"

mkdir -p "$DATA_ROOT" "$EVIDENCE_DIR"/{graph-before,graph-during,graph-after,logs,session}
```

Run evidence must remain outside the repository. Do not store raw audio, transcripts, summaries, raw session bundles, screenshots, browser titles, meeting names, URLs, lyrics, spoken phrases, or notification text in committed files or PR comments. Minimize or sanitize `pw-dump` output before sharing it; raw graph dumps, when needed for debugging, stay local-only under the evidence directory.

## Files

- `SCENARIOS.md` describes the reusable live test scenarios, risks, and pass criteria.
- `RUNBOOK.md` gives repeatable commands for graph snapshots, online smoke runs, and evidence collection.
- `protocol.csv` is the CSV template for recording per-run steps and verdicts.

## Execution Rule

Do not run these tests against private meetings or uncontrolled audio.
Use a closed-room setup, controlled audio sources, and a scratch `MENA_DATA_ROOT`.
Live tests may move playback streams and modify the active PipeWire graph, so they require an explicit test window and operator consent before execution.
