"""Capture worker subprocess entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import IO, TextIO

from mena.capture import (
    DEFAULT_SEGMENT_SECONDS,
    ActivePipeWireCapture,
    CaptureMetadata,
    CaptureRoutePlan,
    CaptureSegment,
    PipeWireCaptureAdapter,
    PipeWireClient,
    SilencePcmProvider,
    WavSegmentWriter,
)
from mena.protocol import (
    ProtocolError,
    ProtocolMessage,
    hello_ack_message,
    read_message,
    validate_hello,
    write_message,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mena.capture_worker")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--session-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_worker(session_id=args.session_id, session_dir=args.session_dir)
    except ProtocolError as exc:
        print(f"protocol error: {exc}", file=sys.stderr)
        return 2


def run_worker(
    *,
    session_id: str,
    session_dir: Path,
    stdin: IO[bytes] | None = None,
    stdout: IO[bytes] | None = None,
    stderr: TextIO | None = None,
) -> int:
    protocol_in = stdin or sys.stdin.buffer
    protocol_out = stdout or sys.stdout.buffer
    diagnostics = stderr or sys.stderr

    hello = read_message(protocol_in)
    validate_hello(hello)
    if hello.session_id != session_id:
        raise ProtocolError("Hello session_id does not match worker session.")
    write_message(protocol_out, hello_ack_message(session_id=session_id, request_id=hello.id))
    capture: WorkerCaptureSession | None = None
    finalized_metadata: CaptureMetadata | None = None

    while True:
        try:
            message = read_message(protocol_in)
        except EOFError:
            if capture and finalized_metadata is None:
                finalized_metadata = capture.finalize()
            print("orchestrator closed protocol stdin", file=diagnostics)
            return 0

        if message.session_id != session_id:
            raise ProtocolError("Message session_id does not match worker session.")
        if message.type == "start_capture":
            if capture is not None:
                raise ProtocolError("Capture has already started.")
            if message.payload.get("simulate_error") is True:
                write_message(
                    protocol_out,
                    ProtocolMessage(
                        type="error",
                        session_id=session_id,
                        payload={"message": "simulated capture start failure"},
                    ),
                )
                return 1
            capture = WorkerCaptureSession(session_dir=session_dir)
            flushed = capture.start()
            write_message(
                protocol_out,
                ProtocolMessage(
                    type="capture_started",
                    session_id=session_id,
                    payload={"session_dir": str(session_dir)},
                ),
            )
            for segment in flushed:
                write_message(
                    protocol_out,
                    ProtocolMessage(
                        type="segment_flushed",
                        session_id=session_id,
                        payload=segment.to_dict(),
                    ),
                )
        elif message.type == "stop_capture":
            if capture is None:
                raise ProtocolError("Cannot stop capture before it has started.")
            if capture and finalized_metadata is None:
                finalized_metadata = capture.finalize()
            if finalized_metadata is None:
                raise ProtocolError("Capture did not finalize.")
            write_message(
                protocol_out,
                ProtocolMessage(
                    type="capture_finalized",
                    session_id=session_id,
                    payload=finalized_metadata.to_dict(),
                ),
            )
            return 0
        else:
            raise ProtocolError(f"Unexpected worker command '{message.type}'.")


class WorkerCaptureSession:
    """Worker-owned deterministic segment writer used by the current PoC slice."""

    def __init__(self, *, session_dir: Path) -> None:
        writer = WavSegmentWriter(segment_dir=session_dir / "artifacts" / "source")
        provider = SilencePcmProvider(seconds=DEFAULT_SEGMENT_SECONDS).read_segment
        self.adapter = PipeWireCaptureAdapter(
            client=NoopPipeWireClient(),
            segment_writer=writer,
            pcm_provider=provider,
        )
        self.segments: list[CaptureSegment] = []
        self.active_capture: ActivePipeWireCapture | None = None
        self.started = False

    def start(self) -> tuple[CaptureSegment, ...]:
        if self.started:
            return ()
        self.started = True
        self.active_capture = self.adapter.start(
            CaptureRoutePlan(
                session_id="worker",
                microphone_route_id="pipewire:node:1",
                playback_stream_route_id="pipewire:node:2",
                output_sink_route_id="pipewire:node:3",
            )
        )
        self.segments.extend(self.active_capture.flush_segments())
        return tuple(self.segments)

    def finalize(self) -> CaptureMetadata:
        if not self.started:
            self.start()
        if self.active_capture is None:
            raise RuntimeError("capture session did not start")
        return self.active_capture.stop()


class NoopPipeWireClient(PipeWireClient):
    """PipeWire client used by deterministic worker tests to avoid graph mutation."""

    def create_null_sink(self, sink_name: str) -> str:
        return f"{sink_name}-module"

    def move_playback_stream(self, playback_stream_route_id: str, sink_name: str) -> None:
        return None

    def create_loopback(self, sink_name: str, output_sink_route_id: str) -> str:
        return f"{sink_name}-loopback"

    def unload_module(self, module_id: str) -> None:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
