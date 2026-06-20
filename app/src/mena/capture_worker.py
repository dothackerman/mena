"""Capture worker subprocess entry point."""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import IO, TextIO

from mena.capture import (
    ActivePipeWireCapture,
    CaptureDiagnosticFn,
    CaptureMetadata,
    CaptureRouteEvent,
    CaptureRoutePlan,
    CaptureSegment,
    CaptureSourceRoute,
    PipeWireCaptureAdapter,
    PipeWireClient,
    SilencePcmProvider,
    WavSegmentWriter,
)
from mena.profiles import CaptureProfileId, CaptureSourceId
from mena.protocol import (
    MAX_FRAME_BYTES,
    CaptureFinalizedPayload,
    CaptureRouteEventPayload,
    CaptureStartedPayload,
    CaptureStatusPayload,
    ErrorPayload,
    ProtocolError,
    ProtocolMessage,
    SegmentFlushedPayload,
    SegmentsFlushedPayload,
    StartCapturePayload,
    hello_ack_message,
    read_message,
    validate_hello,
    write_message,
)

NO_PROCESSABLE_AUDIO_ERROR = "No processable audio segments were sealed."
CAPTURE_FLUSH_ERROR_MESSAGE_MAX_CHARS = 4096


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mena.capture_worker")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument(
        "--test-deterministic-capture",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Write capture diagnostics to stderr.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_worker(
            session_id=args.session_id,
            session_dir=args.session_dir,
            test_deterministic_capture=args.test_deterministic_capture,
            capture_diagnostics=args.verbose,
        )
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
    test_deterministic_capture: bool = False,
    capture_diagnostics: bool = False,
) -> int:
    protocol_in = stdin or sys.stdin.buffer
    protocol_out = stdout or sys.stdout.buffer
    diagnostics = stderr or sys.stderr

    try:
        hello = read_message(protocol_in)
    except EOFError:
        print("orchestrator closed protocol stdin before hello", file=diagnostics)
        return 0
    validate_hello(hello)
    if hello.session_id != session_id:
        raise ProtocolError("Hello session_id does not match worker session.")
    write_message(protocol_out, hello_ack_message(session_id=session_id, request_id=hello.id))
    capture: WorkerCaptureSession | None = None
    finalized_metadata: CaptureMetadata | None = None
    write_lock = threading.Lock()
    flush_loop: SegmentFlushLoop | None = None

    try:
        while True:
            try:
                message = read_message(protocol_in)
            except EOFError:
                if capture and finalized_metadata is None:
                    if flush_loop is not None:
                        flush_loop.stop()
                        flush_loop.join()
                    finalized_metadata = capture.finalize()
                print("orchestrator closed protocol stdin", file=diagnostics)
                return 0

            if message.session_id != session_id:
                raise ProtocolError("Message session_id does not match worker session.")
            if message.type == "start_capture":
                if capture is not None:
                    raise ProtocolError("Capture has already started.")
                start_payload = StartCapturePayload.from_dict(message.payload)
                if test_deterministic_capture:
                    capture = WorkerCaptureSession.create_deterministic(
                        session_id=session_id,
                        session_dir=session_dir,
                        start_payload=start_payload,
                    )
                else:
                    capture = WorkerCaptureSession.create_product(
                        session_id=session_id,
                        session_dir=session_dir,
                        start_payload=start_payload,
                        diagnostics=(
                            lambda message: print(
                                f"[capture-diagnostics] {message}",
                                file=diagnostics,
                            )
                        )
                        if capture_diagnostics
                        else None,
                    )
                capture.start()
                write_message(
                    protocol_out,
                    ProtocolMessage(
                        type="capture_started",
                        session_id=session_id,
                        payload=CaptureStartedPayload(
                            session_dir=str(session_dir),
                            profile=start_payload.profile,
                            segment_seconds=start_payload.segment_seconds,
                            selected_sources=start_payload.selected_sources,
                        ).to_dict(),
                    ),
                )
                _write_route_events(
                    protocol_out=protocol_out,
                    write_lock=write_lock,
                    session_id=session_id,
                    events=_drain_route_events(capture),
                )
                if test_deterministic_capture:
                    _write_flushed_segments(
                        protocol_out=protocol_out,
                        write_lock=write_lock,
                        session_id=session_id,
                        capture=capture,
                    )
                else:
                    flush_loop = SegmentFlushLoop(
                        capture=capture,
                        session_id=session_id,
                        protocol_out=protocol_out,
                        write_lock=write_lock,
                    )
                    flush_loop.start()
            elif message.type == "stop_capture":
                if capture is None:
                    raise ProtocolError("Cannot stop capture before it has started.")
                if capture and finalized_metadata is None:
                    finalized_metadata = _finalize_capture(capture=capture, flush_loop=flush_loop)
                if finalized_metadata is None:
                    raise ProtocolError("Capture did not finalize.")
                finalized_payload = _capture_finalized_payload(
                    finalized_metadata,
                    stop_reason="user_stop",
                )
                write_message(
                    protocol_out,
                    ProtocolMessage(
                        type="capture_finalized",
                        session_id=session_id,
                        payload=finalized_payload.to_dict(),
                    ),
                )
                return 0
            elif message.type == "error":
                error = ErrorPayload.from_dict(message.payload)
                raise ProtocolError(error.message)
            else:
                raise ProtocolError(f"Unexpected worker command '{message.type}'.")
    except BaseException:
        if capture is not None and finalized_metadata is None:
            _cleanup_active_capture(capture=capture, flush_loop=flush_loop)
        raise


class WorkerCaptureSession:
    """Worker-owned capture session."""

    def __init__(
        self,
        *,
        session_id: str,
        start_payload: StartCapturePayload,
        adapter: PipeWireCaptureAdapter,
    ) -> None:
        self.session_id = session_id
        self.start_payload = start_payload
        self.adapter = adapter
        self.segments: list[CaptureSegment] = []
        self.active_capture: ActivePipeWireCapture | None = None
        self.started = False

    @classmethod
    def create_product(
        cls,
        *,
        session_id: str,
        session_dir: Path,
        start_payload: StartCapturePayload,
        diagnostics: CaptureDiagnosticFn | None = None,
    ) -> WorkerCaptureSession:
        writer = WavSegmentWriter(
            segment_dir=session_dir / "artifacts" / "source",
            metadata_root=session_dir,
        )
        return cls(
            session_id=session_id,
            start_payload=start_payload,
            adapter=PipeWireCaptureAdapter(
                client=PipeWireClient(diagnostics=diagnostics),
                segment_writer=writer,
                segment_seconds=start_payload.segment_seconds,
                diagnostics=diagnostics,
            ),
        )

    @classmethod
    def create_deterministic(
        cls,
        *,
        session_id: str,
        session_dir: Path,
        start_payload: StartCapturePayload,
    ) -> WorkerCaptureSession:
        writer = WavSegmentWriter(
            segment_dir=session_dir / "artifacts" / "source",
            metadata_root=session_dir,
        )
        provider = SilencePcmProvider(seconds=start_payload.segment_seconds).read_segment
        return cls(
            session_id=session_id,
            start_payload=start_payload,
            adapter=PipeWireCaptureAdapter(
                client=NoopPipeWireClient(),
                segment_writer=writer,
                pcm_provider=provider,
                segment_seconds=start_payload.segment_seconds,
            ),
        )

    def start(self) -> tuple[CaptureSegment, ...]:
        if self.started:
            return ()
        self.started = True
        profile_id = CaptureProfileId(self.start_payload.profile)
        if profile_id is CaptureProfileId.IN_PERSON_SINGLE_MIC:
            route_plan = CaptureRoutePlan(
                session_id=self.session_id,
                microphone_route_id=self._route_id("microphone"),
                profile_id=profile_id,
                microphone_route_label=self._route_label("microphone"),
                microphone_backend_name=self._route_backend_name("microphone"),
                microphone_pw_record_target=self._route_backend_target("microphone"),
            )
        else:
            route_plan = CaptureRoutePlan(
                session_id=self.session_id,
                microphone_route_id=self._route_id("microphone"),
                playback_stream_route_id=self._route_id("playback_stream"),
                output_sink_route_id=self._route_id("output_sink"),
                profile_id=profile_id,
                microphone_route_label=self._route_label("microphone"),
                playback_stream_route_label=self._route_label("playback_stream"),
                output_sink_route_label=self._route_label("output_sink"),
                microphone_backend_name=self._route_backend_name("microphone"),
                output_sink_backend_name=self._route_backend_name("output_sink"),
                microphone_pw_record_target=self._route_backend_target("microphone"),
                output_sink_pw_record_target=self._route_backend_target("output_sink"),
                playback_stream_sink_input_id=self._route_sink_input_id("playback_stream"),
            )
        self.active_capture = self.adapter.start(
            route_plan,
        )
        return ()

    def flush_once(
        self,
        *,
        stop_event: threading.Event | None = None,
    ) -> tuple[CaptureSegment, ...]:
        if not self.started:
            self.start()
        if self.active_capture is None:
            raise RuntimeError("capture session did not start")
        segments = self.active_capture.flush_segments(stop_event=stop_event)
        self.segments.extend(segments)
        return segments

    def finalize(self) -> CaptureMetadata:
        if not self.started:
            self.start()
        if self.active_capture is None:
            raise RuntimeError("capture session did not start")
        metadata = self.active_capture.stop()
        return CaptureMetadata(
            version=metadata.version,
            profile_id=metadata.profile_id,
            sample_rate=metadata.sample_rate,
            channels=metadata.channels,
            segment_seconds=metadata.segment_seconds,
            segments=metadata.segments,
            source_routes=self._source_routes(),
        )

    def abort(self) -> None:
        if self.active_capture is None:
            return
        self.active_capture.stop()
        self.active_capture = None

    def _route_id(self, role: str) -> str:
        for route in self.start_payload.routes:
            if route.role == role:
                return route.route_id
        raise ProtocolError(f"start_capture payload missing {role} route.")

    def _route_label(self, role: str) -> str:
        for route in self.start_payload.routes:
            if route.role == role:
                return route.label
        raise ProtocolError(f"start_capture payload missing {role} route.")

    def _route_backend_name(self, role: str) -> str | None:
        for route in self.start_payload.routes:
            if route.role == role:
                return route.backend_name
        raise ProtocolError(f"start_capture payload missing {role} route.")

    def _route_backend_target(self, role: str) -> str | None:
        for route in self.start_payload.routes:
            if route.role == role:
                return route.backend_target
        raise ProtocolError(f"start_capture payload missing {role} route.")

    def _route_sink_input_id(self, role: str) -> str | None:
        for route in self.start_payload.routes:
            if route.role == role:
                return route.sink_input_id
        raise ProtocolError(f"start_capture payload missing {role} route.")

    def drain_route_events(self) -> tuple[CaptureRouteEvent, ...]:
        if self.active_capture is None:
            return ()
        return self.active_capture.drain_route_events()

    def _source_routes(self) -> tuple[CaptureSourceRoute, ...]:
        source_routes: list[CaptureSourceRoute] = []
        for route in self.start_payload.routes:
            if route.source_id is None:
                continue
            source_routes.append(
                CaptureSourceRoute(
                    source_id=CaptureSourceId(route.source_id),
                    route_id=route.route_id,
                    route_label=route.label,
                    capture_mode=(
                        "selected_microphone" if route.role == "microphone" else "dedicated_route"
                    ),
                )
            )
        return tuple(source_routes)


class SegmentFlushLoop:
    """Background source-segment flusher for product capture."""

    def __init__(
        self,
        *,
        capture: WorkerCaptureSession,
        session_id: str,
        protocol_out: IO[bytes],
        write_lock: threading.Lock,
    ) -> None:
        self.capture = capture
        self.session_id = session_id
        self.protocol_out = protocol_out
        self.write_lock = write_lock
        self.error: BaseException | None = None
        self._started_at: float | None = None
        self._stop_requested = threading.Event()
        self._thread = threading.Thread(target=self._run, name="mena-capture-flush", daemon=True)

    def start(self) -> None:
        self._started_at = time.monotonic()
        self._thread.start()

    def stop(self) -> None:
        self._stop_requested.set()

    def join(self) -> None:
        self._thread.join()

    def _run(self) -> None:
        while not self._stop_requested.is_set():
            try:
                _write_flushed_segments(
                    protocol_out=self.protocol_out,
                    write_lock=self.write_lock,
                    session_id=self.session_id,
                    capture=self.capture,
                    stop_event=self._stop_requested,
                )
                with self.write_lock:
                    write_message(
                        self.protocol_out,
                        ProtocolMessage(
                            type="capture_status",
                            session_id=self.session_id,
                            payload=CaptureStatusPayload(
                                status="recording",
                                elapsed_seconds=self._elapsed_seconds(),
                                sealed_segment_count=len(self.capture.segments),
                            ).to_dict(),
                        ),
                    )
            except BaseException as exc:
                self.error = exc
                _cleanup_active_capture(capture=self.capture, flush_loop=None)
                with self.write_lock:
                    write_message(
                        self.protocol_out,
                        ProtocolMessage(
                            type="error",
                            session_id=self.session_id,
                            payload=ErrorPayload(
                                code="capture_flush_failed",
                                message=_bounded_error_message(exc),
                                recoverable=False,
                                details={},
                            ).to_dict(),
                        ),
                    )
                return

    def _elapsed_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        return max(0.0, time.monotonic() - self._started_at)


def _bounded_error_message(exc: BaseException) -> str:
    message = str(exc) or exc.__class__.__name__
    if len(message) <= CAPTURE_FLUSH_ERROR_MESSAGE_MAX_CHARS:
        return message
    suffix = f"... [truncated to keep protocol frame below {MAX_FRAME_BYTES} bytes]"
    return message[:CAPTURE_FLUSH_ERROR_MESSAGE_MAX_CHARS] + suffix


def _write_flushed_segments(
    *,
    protocol_out: IO[bytes],
    write_lock: threading.Lock,
    session_id: str,
    capture: WorkerCaptureSession,
    stop_event: threading.Event | None = None,
) -> None:
    flushed = capture.flush_once(stop_event=stop_event)
    _write_route_events(
        protocol_out=protocol_out,
        write_lock=write_lock,
        session_id=session_id,
        events=_drain_route_events(capture),
    )
    for segment in flushed:
        SegmentFlushedPayload.from_dict(segment.to_dict())
    if flushed:
        with write_lock:
            write_message(
                protocol_out,
                ProtocolMessage(
                    type="segments_flushed",
                    session_id=session_id,
                    payload=SegmentsFlushedPayload(
                        segments=tuple(
                            SegmentFlushedPayload.from_dict(segment.to_dict())
                            for segment in flushed
                        )
                    ).to_dict(),
                ),
            )


def _drain_route_events(capture: WorkerCaptureSession) -> tuple[CaptureRouteEvent, ...]:
    drain = getattr(capture, "drain_route_events", None)
    if not callable(drain):
        return ()
    return tuple(drain())


def _finalize_capture(
    *,
    capture: WorkerCaptureSession,
    flush_loop: SegmentFlushLoop | None,
) -> CaptureMetadata:
    if flush_loop is not None:
        flush_loop.stop()
        flush_loop.join()
        try:
            metadata = capture.finalize()
        finally:
            if flush_loop.error is not None:
                raise ProtocolError(f"Capture failed: {flush_loop.error}")
        return metadata
    return capture.finalize()


def _capture_finalized_payload(
    metadata: CaptureMetadata,
    *,
    stop_reason: str,
) -> CaptureFinalizedPayload:
    if metadata.segments:
        return CaptureFinalizedPayload(
            status="recorded",
            stop_reason=stop_reason,
            completed_at=None,
            error=None,
            capture_metadata=metadata.to_summary_dict(),
        )
    return CaptureFinalizedPayload(
        status="interrupted_partial",
        stop_reason=stop_reason,
        completed_at=None,
        error=NO_PROCESSABLE_AUDIO_ERROR,
        capture_metadata=metadata.to_summary_dict(),
    )


def _cleanup_active_capture(
    *,
    capture: WorkerCaptureSession | None,
    flush_loop: SegmentFlushLoop | None,
) -> None:
    if flush_loop is not None:
        flush_loop.stop()
        flush_loop.join()
    if capture is None:
        return
    abort = getattr(capture, "abort", None)
    if callable(abort):
        with suppress(BaseException):
            abort()
        return
    with suppress(BaseException):
        capture.finalize()


def _write_route_events(
    *,
    protocol_out: IO[bytes],
    write_lock: threading.Lock,
    session_id: str,
    events: tuple[CaptureRouteEvent, ...],
) -> None:
    for event in events:
        with write_lock:
            write_message(
                protocol_out,
                ProtocolMessage(
                    type="capture_route_event",
                    session_id=session_id,
                    payload=CaptureRouteEventPayload.from_dict(event.to_dict()).to_dict(),
                ),
            )


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
