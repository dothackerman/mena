# Live PipeWire UAT Runbook

Status: manual runbook for privacy-preserving Linux live audio evidence

Use this runbook with the scenarios in `SCENARIOS.md`. Keep run evidence outside the repository.

## Setup

```sh
RUN_ID="$(date -u +%Y%m%d-%H%M%S)-kai-uat"
DATA_ROOT="/tmp/mena-live-smoke-data/$RUN_ID"
EVIDENCE_DIR="/home/oriol/.openclaw/workspace/architect/artifacts/mena-live-pipewire-smoke/runs/$RUN_ID"

mkdir -p "$DATA_ROOT" "$EVIDENCE_DIR"/{graph-before,graph-during,graph-after,logs,session}
```

## Graph Snapshots

```sh
capture_graph_snapshot() {
  phase="$1"
  out="$EVIDENCE_DIR/graph-$phase"
  mkdir -p "$out"

  date --iso-8601=seconds > "$out/timestamp.txt"
  pactl list short sinks > "$out/pactl-sinks.short.txt"
  pactl list short sources > "$out/pactl-sources.short.txt"
  pactl list short sink-inputs > "$out/pactl-sink-inputs.short.txt"
  pactl list short modules > "$out/pactl-modules.short.txt"
  pw-link -l > "$out/pw-link.txt"

  pw-dump | jq '
    [.[] | select(
      .type == "PipeWire:Interface:Node" or
      .type == "PipeWire:Interface:Port" or
      .type == "PipeWire:Interface:Link"
    ) | {
      id,
      type,
      props: ((.info.props // {}) | with_entries(select(.key | test(
        "^(object.serial|object.path|media.class|node.name|node.nick|node.id|client.id|port.id|port.name|audio.position|link.output.node|link.input.node|target.object)$"
      ))))
    }]
  ' > "$out/pw-dump-minimal.json"
}
```

Capture `before`, `during`, and `after` for each mutating online run. The `during` snapshot means after Mena reports that recording has started and before stop/finalization.

## Discovery

```sh
capture_graph_snapshot before

MENA_DATA_ROOT="$DATA_ROOT" mena record \
  --profile online \
  --language en \
  --dry-run \
  2>&1 | tee "$EVIDENCE_DIR/logs/discovery-online.log"
```

## Online Smoke

Use controlled local microphone input and controlled playback with intentionally different source signals.

```sh
MIC_ROUTE="pipewire:node:<mic-node-id>"
PLAYBACK_STREAM="pulse:sink-input:<active-playback-id>"
OUTPUT_SINK="pipewire:node:<output-sink-node-id>"

MENA_DATA_ROOT="$DATA_ROOT" mena record \
  --profile online \
  --language en \
  --mic "$MIC_ROUTE" \
  --playback-stream "$PLAYBACK_STREAM" \
  --output-sink "$OUTPUT_SINK" \
  --verbose \
  2>&1 | tee "$EVIDENCE_DIR/logs/record-online.log"
```

In another terminal while recording:

```sh
capture_graph_snapshot during
```

Immediately after stop/finalization:

```sh
capture_graph_snapshot after

pgrep -af 'mena.capture_worker|pw-record|pw-cat|pw-loopback' \
  > "$EVIDENCE_DIR/logs/post-run-processes.txt" || true
```

## Segment Inventory

```sh
find "$DATA_ROOT/sessions" -type f -name '*.wav' -printf '%P,%s bytes\n' \
  | sort > "$EVIDENCE_DIR/session/segment-files.csv"

find "$DATA_ROOT/sessions" -type f -name '*.wav' -print0 \
  | sort -z \
  | xargs -0 sha256sum \
  > "$EVIDENCE_DIR/session/segment-sha256.txt"
```

## Kai Rerun After Fix

- Install the fixed release or wheel first; use plain `mena`, not `uv run mena`.
- Create a fresh `RUN_ID`, `DATA_ROOT`, and `EVIDENCE_DIR`.
- Run S1 readiness, then S3 online dedicated-route smoke.
- Use controlled local mic input and controlled playback with intentionally different source signals.
- Capture `before`, `during`, and `after` graph snapshots every time.
- Share only the verdict summary and evidence directory path in the PR or UAT thread.
- Do not paste raw `pw-dump`, audio hashes tied to private files, content labels, transcript text, meeting/source titles, browser URLs, or screenshots.

Given the current known source-independence failure, S3 is the required repeat after the fix. Repeat S4 only if the fix touches fallback or degradation routing.
