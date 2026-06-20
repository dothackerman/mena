import subprocess
import sys
import threading
import time
import wave
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, cast

import pytest

from mena.capture import CaptureMetadata, CaptureRouteEvent, CaptureSegment
from mena.capture_worker import (
    SegmentFlushLoop,
    WorkerCaptureSession,
    _cleanup_active_capture,
    build_parser,
    run_worker,
)
from mena.profiles import CaptureProfileId, CaptureSourceId
from mena.protocol import (
    ENVELOPE_VERSION,
    MAX_FRAME_BYTES,
    PROTOCOL_MAJOR,
    PROTOCOL_MINOR,
    CaptureFinalizedPayload,
    CaptureRouteBindingPayload,
    CaptureRouteEventPayload,
    CaptureStatusPayload,
    ErrorPayload,
    ProtocolError,
    ProtocolMessage,
    SegmentFlushedPayload,
    SegmentsFlushedPayload,
    StartCapturePayload,
    WorkerExitedError,
    WorkerProcess,
    hello_message,
    read_message,
    validate_hello,
)


def test_protocol_message_encodes_compact_json_line() -> None:
    message = ProtocolMessage(
        type="start_capture",
        id="message-1",
        session_id="session-1",
        payload=_start_payload().to_dict(),
    )

    encoded = message.encode()

    assert encoded.endswith(b"\n")
    assert b": " not in encoded
    assert b", " not in encoded
    assert ProtocolMessage.decode(encoded) == message
    assert ProtocolMessage.decode(encoded).v == ENVELOPE_VERSION


def test_protocol_encode_rejects_oversized_frame() -> None:
    with pytest.raises(ProtocolError, match="exceeds"):
        ProtocolMessage(
            type="stop_capture",
            session_id="session-1",
            payload={"data": "x" * MAX_FRAME_BYTES},
        ).encode()


def test_protocol_minor_is_bumped_for_issue_83_contract() -> None:
    assert PROTOCOL_MINOR == 1


def test_protocol_encode_rejects_unsupported_envelope_version() -> None:
    with pytest.raises(ProtocolError, match="Unsupported protocol envelope version"):
        ProtocolMessage(type="hello", session_id="session-1", v=ENVELOPE_VERSION + 1).encode()


@pytest.mark.parametrize(
    ("message", "error"),
    [
        (ProtocolMessage(type="hello", id="", session_id="session-1"), "id"),
        (ProtocolMessage(type="hello", session_id=""), "session_id"),
        (
            ProtocolMessage(type="hello", session_id="session-1", payload=None),  # type: ignore[arg-type]
            "payload",
        ),
    ],
)
def test_protocol_encode_rejects_invalid_outbound_fields(
    message: ProtocolMessage,
    error: str,
) -> None:
    with pytest.raises(ProtocolError, match=error):
        message.encode()


def test_protocol_encode_rejects_non_json_payload() -> None:
    with pytest.raises(ProtocolError, match="JSON serializable"):
        ProtocolMessage(
            type="hello",
            session_id="session-1",
            payload={"path": Path("not-json")},
        ).encode()


def test_start_capture_payload_rejects_legacy_profile_only_shape() -> None:
    with pytest.raises(ProtocolError, match="missing required field 'language'"):
        ProtocolMessage(
            type="start_capture",
            session_id="session-1",
            payload={"profile": "online_dual_stream"},
        ).encode()


def test_start_capture_payload_rejects_extra_fields() -> None:
    payload = _start_payload().to_dict()
    payload["simulate_error"] = True

    with pytest.raises(ProtocolError, match="unsupported field 'simulate_error'"):
        ProtocolMessage(
            type="start_capture",
            session_id="session-1",
            payload=payload,
        ).encode()


def test_error_payload_rejects_unstructured_errors() -> None:
    with pytest.raises(ProtocolError, match="missing required field 'code'"):
        ProtocolMessage(
            type="error",
            session_id="session-1",
            payload={"message": "boom"},
        ).encode()


def test_capture_status_payload_round_trips_through_protocol_validation() -> None:
    payload = CaptureStatusPayload(
        status="recording",
        elapsed_seconds=12.5,
        sealed_segment_count=4,
    ).to_dict()

    decoded = ProtocolMessage.decode(
        ProtocolMessage(
            type="capture_status",
            session_id="session-1",
            payload=payload,
        ).encode()
    )

    assert CaptureStatusPayload.from_dict(decoded.payload) == CaptureStatusPayload(
        status="recording",
        elapsed_seconds=12.5,
        sealed_segment_count=4,
    )


@pytest.mark.parametrize(
    ("payload_update", "error"),
    [
        ({"status": "paused"}, "must be one of"),
        ({"elapsed_seconds": True}, "must be a number"),
        ({"elapsed_seconds": -0.1}, "must be non-negative"),
        ({"sealed_segment_count": -1}, "must be non-negative"),
    ],
)
def test_capture_status_payload_rejects_invalid_values(
    payload_update: dict[str, object],
    error: str,
) -> None:
    payload = CaptureStatusPayload(
        status="recording",
        elapsed_seconds=0.0,
        sealed_segment_count=0,
    ).to_dict()
    payload.update(payload_update)

    with pytest.raises(ProtocolError, match=error):
        ProtocolMessage(
            type="capture_status",
            session_id="session-1",
            payload=payload,
        ).encode()


def test_capture_route_event_payload_round_trips_through_protocol_validation() -> None:
    payload = CaptureRouteEventPayload(
        event="degraded",
        code="dedicated_route_unavailable",
        message="Using selected output sink monitor.",
        source_id="system_audio",
        occurred_at=None,
        elapsed_seconds=0.0,
        segment_index=1,
        route_id="pipewire:node:42",
        route_label="Headphones",
        capture_mode="system_mix_remote",
    ).to_dict()

    decoded = ProtocolMessage.decode(
        ProtocolMessage(
            type="capture_route_event",
            session_id="session-1",
            payload=payload,
        ).encode()
    )

    assert CaptureRouteEventPayload.from_dict(decoded.payload).capture_mode == ("system_mix_remote")


@pytest.mark.parametrize(
    ("payload_update", "error"),
    [
        ({"event": "warning"}, "must be one of"),
        ({"source_id": "mixed_audio"}, "must be one of"),
        ({"elapsed_seconds": -1.0}, "must be non-negative"),
        ({"segment_index": 0}, "must be positive"),
        ({"capture_mode": "mic_only"}, "must be one of"),
    ],
)
def test_capture_route_event_payload_rejects_invalid_values(
    payload_update: dict[str, object],
    error: str,
) -> None:
    payload = CaptureRouteEventPayload(
        event="degraded",
        code="dedicated_route_unavailable",
        message="Using selected output sink monitor.",
        source_id="system_audio",
        occurred_at=None,
        elapsed_seconds=0.0,
        segment_index=1,
        route_id="pipewire:node:42",
        route_label="Headphones",
        capture_mode="system_mix_remote",
    ).to_dict()
    payload.update(payload_update)

    with pytest.raises(ProtocolError, match=error):
        ProtocolMessage(
            type="capture_route_event",
            session_id="session-1",
            payload=payload,
        ).encode()


def test_segment_flushed_payload_round_trips_through_protocol_validation() -> None:
    payload = _segment_payload(source_id="local_mic")

    decoded = ProtocolMessage.decode(
        ProtocolMessage(
            type="segment_flushed",
            session_id="session-1",
            payload=payload,
        ).encode()
    )

    assert SegmentFlushedPayload.from_dict(decoded.payload).source_id == "local_mic"


def test_segments_flushed_payload_round_trips_aligned_batch() -> None:
    payload: dict[str, object] = {
        "segments": [
            _segment_payload(source_id="local_mic"),
            _segment_payload(source_id="system_audio"),
        ]
    }

    decoded = ProtocolMessage.decode(
        ProtocolMessage(
            type="segments_flushed",
            session_id="session-1",
            payload=payload,
        ).encode()
    )

    batch = SegmentsFlushedPayload.from_dict(decoded.payload)
    assert [segment.source_id for segment in batch.segments] == [
        "local_mic",
        "system_audio",
    ]


@pytest.mark.parametrize(
    ("payload_update", "error"),
    [
        ({"format": "flac"}, "format must be 'wav'"),
        ({"source_id": "mixed_audio"}, "must be one of"),
        ({"sha256": "A" * 64}, "lowercase SHA-256"),
        ({"started_at": ""}, "non-empty string or null"),
    ],
)
def test_segment_flushed_payload_rejects_invalid_values(
    payload_update: dict[str, object],
    error: str,
) -> None:
    payload = _segment_payload(source_id="local_mic")
    payload.update(payload_update)

    with pytest.raises(ProtocolError, match=error):
        ProtocolMessage(
            type="segment_flushed",
            session_id="session-1",
            payload=payload,
        ).encode()


@pytest.mark.parametrize(
    ("metadata_update", "error"),
    [
        ({"version": 2}, "version' must be 1"),
        ({"segment_count": 2}, "segment_count must match"),
        ({"segments": [7]}, "segments must contain JSON objects"),
        ({"sources": [7]}, "sources must contain JSON objects"),
    ],
)
def test_capture_finalized_payload_rejects_invalid_metadata(
    metadata_update: dict[str, object],
    error: str,
) -> None:
    metadata = _capture_metadata()
    metadata.update(metadata_update)
    payload = CaptureFinalizedPayload(
        status="recorded",
        stop_reason="user_stop",
        completed_at=None,
        error=None,
        capture_metadata=metadata,
    ).to_dict()

    with pytest.raises(ProtocolError, match=error):
        ProtocolMessage(
            type="capture_finalized",
            session_id="session-1",
            payload=payload,
        ).encode()


def test_error_payload_round_trips_details() -> None:
    payload = ErrorPayload(
        code="capture_flush_failed",
        message="boom",
        recoverable=False,
        details={"source_id": "local_mic"},
    ).to_dict()

    decoded = ProtocolMessage.decode(
        ProtocolMessage(type="error", session_id="session-1", payload=payload).encode()
    )

    assert ErrorPayload.from_dict(decoded.payload).details == {"source_id": "local_mic"}


def test_protocol_rejects_unknown_message_type() -> None:
    line = (
        b'{"v":1,"type":"pause_capture","id":"message-1","session_id":"session-1","payload":{}}\n'
    )

    with pytest.raises(ProtocolError, match="Unknown protocol message type 'pause_capture'"):
        ProtocolMessage.decode(line)


def test_protocol_encode_rejects_unknown_message_type() -> None:
    with pytest.raises(ProtocolError, match="Unknown protocol message type 'pause_capture'"):
        ProtocolMessage(type="pause_capture", session_id="session-1").encode()


@pytest.mark.parametrize(
    ("line", "message"),
    [
        (b'{"v":1\n', "valid JSON"),
        (b"[]\n", "JSON object"),
        (
            b'{"v":1,"type":"hello","id":"message-1","session_id":"session-1"}\n',
            "payload",
        ),
        (
            b'{"v":"1","type":"hello","id":"message-1","session_id":"session-1","payload":{}}\n',
            "integer",
        ),
        (
            b'{"v":1,"type":"hello","id":"message-1","session_id":"","payload":{}}\n',
            "non-empty string",
        ),
        (b"\xff\n", "valid UTF-8"),
    ],
)
def test_protocol_rejects_malformed_frames(line: bytes, message: str) -> None:
    with pytest.raises(ProtocolError, match=message):
        ProtocolMessage.decode(line)


def test_protocol_rejects_oversized_frame() -> None:
    with pytest.raises(ProtocolError, match="exceeds"):
        ProtocolMessage.decode(b"{" + (b'"x":' + b'"a"' * MAX_FRAME_BYTES) + b"}\n")


def test_read_message_rejects_truncated_frame(tmp_path: Path) -> None:
    path = tmp_path / "frame.jsonl"
    path.write_bytes(
        b'{"v":1,"type":"hello","id":"message-1","session_id":"session-1","payload":{}}'
    )

    with path.open("rb") as stream, pytest.raises(ProtocolError, match="LF terminated"):
        read_message(stream)


def test_hello_version_mismatch_is_protocol_error() -> None:
    message = hello_message("session-1")
    bad_hello = ProtocolMessage(
        type=message.type,
        id=message.id,
        session_id=message.session_id,
        payload={**message.payload, "protocol_major": PROTOCOL_MAJOR + 1},
    )

    with pytest.raises(ProtocolError, match="Unsupported protocol major version"):
        validate_hello(bad_hello)


def test_protocol_rejects_unsupported_envelope_version() -> None:
    line = b'{"v":99,"type":"hello","id":"message-1","session_id":"session-1","payload":{}}\n'

    with pytest.raises(ProtocolError, match="Unsupported protocol envelope version"):
        ProtocolMessage.decode(line)


def test_validate_hello_requires_hello_type_and_worker_role() -> None:
    with pytest.raises(ProtocolError, match="First protocol message must be hello"):
        validate_hello(ProtocolMessage(type="start_capture", session_id="session-1"))

    with pytest.raises(ProtocolError, match="Hello role must be 'worker'"):
        validate_hello(
            ProtocolMessage(
                type="hello",
                session_id="session-1",
                payload={"protocol_major": PROTOCOL_MAJOR, "protocol_minor": 0, "role": "host"},
            )
        )


def test_read_message_reports_eof() -> None:
    with pytest.raises(EOFError, match="closed the stream"):
        read_message(BytesIO())


def test_capture_worker_parser_requires_session_fields(tmp_path: Path) -> None:
    args = build_parser().parse_args(["--session-id", "session-1", "--session-dir", str(tmp_path)])

    assert args.session_id == "session-1"
    assert args.session_dir == tmp_path


def test_worker_process_handshake_start_stop_round_trip(tmp_path: Path) -> None:
    with WorkerProcess(
        session_id="session-1",
        session_dir=tmp_path,
        argv=_deterministic_worker_argv(session_id="session-1", session_dir=tmp_path),
    ) as worker:
        hello_ack = worker.handshake()
        started = worker.start_capture(_start_payload())
        finalized = worker.stop_capture()
        exit_code = worker.wait(timeout=2)
        stderr = worker.read_stderr()
    finalized_payload = CaptureFinalizedPayload.from_dict(finalized.payload)

    assert hello_ack.type == "hello_ack"
    assert started.payload == {
        "session_dir": str(tmp_path),
        "profile": "online_dual_stream",
        "segment_seconds": 30,
        "selected_sources": ["local_mic", "system_audio"],
    }
    assert finalized_payload.status == "recorded"
    assert finalized_payload.capture_metadata["segment_count"] == 2
    assert "segments" not in finalized_payload.capture_metadata
    sources = finalized_payload.capture_metadata["sources"]
    assert isinstance(sources, list)
    assert sources[0]["route_id"] == "pipewire:node:1"
    assert sources[0]["route_label"] == "Headset microphone"
    assert sources[1]["route_id"] == "pulse:sink-input:2"
    assert sources[1]["route_label"] == "Meeting playback"
    assert exit_code == 0
    assert stderr == ""
    assert (tmp_path / "artifacts" / "source" / "local_mic" / "local_mic_000001.wav").exists()
    assert (tmp_path / "artifacts" / "source" / "system_audio" / "system_audio_000001.wav").exists()


def test_worker_process_handshake_rejects_mismatched_ack_id(tmp_path: Path) -> None:
    with (
        WorkerProcess(
            session_id="session-1",
            session_dir=tmp_path,
            argv=[
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "sys.stdin.readline(); "
                    "sys.stdout.write("
                    '\'{"v":1,"type":"hello_ack","id":"wrong",'
                    '"session_id":"session-1","payload":{"accepted":true}}\\n\''
                    "); "
                    "sys.stdout.flush()"
                ),
            ],
        ) as worker,
        pytest.raises(ProtocolError, match="hello_ack id does not match"),
    ):
        worker.handshake()


def test_worker_process_handshake_rejects_mismatched_ack_session(tmp_path: Path) -> None:
    with (
        WorkerProcess(
            session_id="session-1",
            session_dir=tmp_path,
            argv=[
                sys.executable,
                "-c",
                (
                    "import sys, json; "
                    "hello=json.loads(sys.stdin.readline()); "
                    "hello['type']='hello_ack'; "
                    "hello['session_id']='session-2'; "
                    "hello['payload']={'accepted': True}; "
                    "sys.stdout.write(json.dumps(hello) + '\\n'); "
                    "sys.stdout.flush()"
                ),
            ],
        ) as worker,
        pytest.raises(ProtocolError, match="hello_ack session_id does not match"),
    ):
        worker.handshake()


def test_worker_run_loop_handles_start_stop_with_in_memory_streams(tmp_path: Path) -> None:
    stdin = BytesIO(
        b"".join(
            [
                hello_message("session-1").encode(),
                ProtocolMessage(
                    type="start_capture",
                    session_id="session-1",
                    payload=_start_payload().to_dict(),
                ).encode(),
                ProtocolMessage(type="stop_capture", session_id="session-1").encode(),
            ]
        )
    )
    stdout = BytesIO()
    stderr = StringIO()

    exit_code = run_worker(
        session_id="session-1",
        session_dir=tmp_path,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        test_deterministic_capture=True,
    )

    frames = stdout.getvalue().splitlines(keepends=True)
    assert exit_code == 0
    assert [ProtocolMessage.decode(frame).type for frame in frames] == [
        "hello_ack",
        "capture_started",
        "segments_flushed",
        "capture_finalized",
    ]
    assert stderr.getvalue() == ""


def test_worker_run_loop_emits_capture_route_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RouteEventCapture:
        def __init__(self) -> None:
            self.segments: list[CaptureSegment] = []
            self._events = [
                CaptureRouteEvent(
                    event="degraded",
                    code="dedicated_route_unavailable",
                    message="Using selected output sink monitor.",
                    source_id=CaptureSourceId.SYSTEM_AUDIO,
                    route_id="pipewire:node:3",
                    route_label="Headphones",
                    capture_mode="system_mix_remote",
                    elapsed_seconds=0.0,
                    segment_index=1,
                )
            ]

        def start(self) -> tuple[CaptureSegment, ...]:
            return ()

        def flush_once(
            self,
            *,
            stop_event: threading.Event | None = None,
        ) -> tuple[CaptureSegment, ...]:
            return ()

        def finalize(self) -> CaptureMetadata:
            return CaptureMetadata(
                version=1,
                profile_id=CaptureProfileId.ONLINE_DUAL_STREAM,
                sample_rate=48_000,
                channels=1,
                segment_seconds=30,
                segments=(),
            )

        def drain_route_events(self) -> tuple[CaptureRouteEvent, ...]:
            events = tuple(self._events)
            self._events.clear()
            return events

    monkeypatch.setattr(
        WorkerCaptureSession,
        "create_deterministic",
        classmethod(lambda *_args, **_kwargs: RouteEventCapture()),
    )
    stdin = BytesIO(
        b"".join(
            [
                hello_message("session-1").encode(),
                ProtocolMessage(
                    type="start_capture",
                    session_id="session-1",
                    payload=_start_payload().to_dict(),
                ).encode(),
                ProtocolMessage(type="stop_capture", session_id="session-1").encode(),
            ]
        )
    )
    stdout = BytesIO()

    exit_code = run_worker(
        session_id="session-1",
        session_dir=tmp_path,
        stdin=stdin,
        stdout=stdout,
        test_deterministic_capture=True,
    )

    frames = [ProtocolMessage.decode(frame) for frame in stdout.getvalue().splitlines(True)]
    route_event = CaptureRouteEventPayload.from_dict(frames[2].payload)
    assert exit_code == 0
    assert [frame.type for frame in frames] == [
        "hello_ack",
        "capture_started",
        "capture_route_event",
        "capture_finalized",
    ]
    assert route_event.capture_mode == "system_mix_remote"


def test_worker_run_loop_stops_product_flush_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingProductCapture:
        def __init__(self) -> None:
            self.segments: list[CaptureSegment] = []
            self.finalized = False

        def start(self) -> tuple[CaptureSegment, ...]:
            return ()

        def flush_once(
            self,
            *,
            stop_event: threading.Event | None = None,
        ) -> tuple[CaptureSegment, ...]:
            while stop_event is not None and not stop_event.is_set():
                time.sleep(0.01)
            return ()

        def finalize(self) -> CaptureMetadata:
            self.finalized = True
            return CaptureMetadata(
                version=1,
                profile_id=CaptureProfileId.ONLINE_DUAL_STREAM,
                sample_rate=48_000,
                channels=1,
                segment_seconds=30,
                segments=(),
            )

        def drain_route_events(self) -> tuple[CaptureRouteEvent, ...]:
            return ()

    capture = BlockingProductCapture()
    monkeypatch.setattr(
        WorkerCaptureSession,
        "create_product",
        classmethod(lambda *_args, **_kwargs: capture),
    )
    stdin = BytesIO(
        b"".join(
            [
                hello_message("session-1").encode(),
                ProtocolMessage(
                    type="start_capture",
                    session_id="session-1",
                    payload=_start_payload().to_dict(),
                ).encode(),
                ProtocolMessage(type="stop_capture", session_id="session-1").encode(),
            ]
        )
    )
    stdout = BytesIO()

    exit_code = run_worker(
        session_id="session-1",
        session_dir=tmp_path,
        stdin=stdin,
        stdout=stdout,
    )

    frames = [ProtocolMessage.decode(frame) for frame in stdout.getvalue().splitlines(True)]
    finalized = CaptureFinalizedPayload.from_dict(frames[-1].payload)
    assert exit_code == 0
    assert capture.finalized
    assert frames[0].type == "hello_ack"
    assert frames[1].type == "capture_started"
    assert frames[-1].type == "capture_finalized"
    assert finalized.status == "interrupted_partial"
    assert finalized.error == "No processable audio segments were sealed."


def test_worker_run_loop_cleans_up_active_capture_on_orchestrator_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AbortableProductCapture:
        def __init__(self) -> None:
            self.segments: list[CaptureSegment] = []
            self.aborted = False

        def start(self) -> tuple[CaptureSegment, ...]:
            return ()

        def flush_once(
            self,
            *,
            stop_event: threading.Event | None = None,
        ) -> tuple[CaptureSegment, ...]:
            while stop_event is not None and not stop_event.is_set():
                time.sleep(0.01)
            return ()

        def abort(self) -> None:
            self.aborted = True

        def drain_route_events(self) -> tuple[CaptureRouteEvent, ...]:
            return ()

    capture = AbortableProductCapture()
    monkeypatch.setattr(
        WorkerCaptureSession,
        "create_product",
        classmethod(lambda *_args, **_kwargs: capture),
    )
    stdin = BytesIO(
        b"".join(
            [
                hello_message("session-1").encode(),
                ProtocolMessage(
                    type="start_capture",
                    session_id="session-1",
                    payload=_start_payload().to_dict(),
                ).encode(),
                ProtocolMessage(
                    type="error",
                    session_id="session-1",
                    payload={
                        "code": "orchestrator_aborted",
                        "message": "orchestrator aborted",
                        "recoverable": False,
                        "details": {},
                    },
                ).encode(),
            ]
        )
    )

    with pytest.raises(ProtocolError, match="orchestrator aborted"):
        run_worker(
            session_id="session-1",
            session_dir=tmp_path,
            stdin=stdin,
            stdout=BytesIO(),
        )

    assert capture.aborted


def test_worker_run_loop_cleans_up_active_capture_on_unexpected_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AbortableProductCapture:
        def __init__(self) -> None:
            self.segments: list[CaptureSegment] = []
            self.aborted = False

        def start(self) -> tuple[CaptureSegment, ...]:
            return ()

        def flush_once(
            self,
            *,
            stop_event: threading.Event | None = None,
        ) -> tuple[CaptureSegment, ...]:
            while stop_event is not None and not stop_event.is_set():
                time.sleep(0.01)
            return ()

        def abort(self) -> None:
            self.aborted = True

        def drain_route_events(self) -> tuple[CaptureRouteEvent, ...]:
            return ()

    capture = AbortableProductCapture()
    monkeypatch.setattr(
        WorkerCaptureSession,
        "create_product",
        classmethod(lambda *_args, **_kwargs: capture),
    )
    stdin = BytesIO(
        b"".join(
            [
                hello_message("session-1").encode(),
                ProtocolMessage(
                    type="start_capture",
                    session_id="session-1",
                    payload=_start_payload().to_dict(),
                ).encode(),
                ProtocolMessage(
                    type="capture_status",
                    session_id="session-1",
                    payload=CaptureStatusPayload(
                        status="recording",
                        elapsed_seconds=1.0,
                        sealed_segment_count=0,
                    ).to_dict(),
                ).encode(),
            ]
        )
    )

    with pytest.raises(ProtocolError, match="Unexpected worker command 'capture_status'"):
        run_worker(
            session_id="session-1",
            session_dir=tmp_path,
            stdin=stdin,
            stdout=BytesIO(),
        )

    assert capture.aborted


def test_worker_cleanup_active_capture_falls_back_to_finalize() -> None:
    class FinalizeOnlyCapture:
        def __init__(self) -> None:
            self.finalized = False

        def finalize(self) -> CaptureMetadata:
            self.finalized = True
            return CaptureMetadata(
                version=1,
                profile_id=CaptureProfileId.ONLINE_DUAL_STREAM,
                sample_rate=48_000,
                channels=1,
                segment_seconds=30,
                segments=(),
            )

    capture = FinalizeOnlyCapture()

    _cleanup_active_capture(capture=None, flush_loop=None)
    _cleanup_active_capture(capture=cast(WorkerCaptureSession, capture), flush_loop=None)

    assert capture.finalized


def test_worker_product_factory_uses_live_pipewire_provider(tmp_path: Path) -> None:
    capture = WorkerCaptureSession.create_product(
        session_id="session-1",
        session_dir=tmp_path,
        start_payload=_start_payload(),
    )

    assert capture.session_id == "session-1"
    assert capture.adapter.pcm_provider is None
    assert capture.start_payload.selected_sources == ("local_mic", "system_audio")


def test_worker_capture_session_flush_starts_lazily(tmp_path: Path) -> None:
    capture = WorkerCaptureSession.create_deterministic(
        session_id="session-room",
        session_dir=tmp_path,
        start_payload=_in_person_start_payload(),
    )

    segments = capture.flush_once()
    metadata = capture.finalize()

    assert [segment.source_id for segment in segments] == [CaptureSourceId.ROOM_MIC]
    assert metadata.profile_id.value == "in_person_single_mic"
    assert metadata.segments == segments


def test_worker_capture_session_start_is_idempotent(tmp_path: Path) -> None:
    capture = WorkerCaptureSession.create_deterministic(
        session_id="session-1",
        session_dir=tmp_path,
        start_payload=_start_payload(),
    )

    assert capture.start() == ()
    assert capture.active_capture is not None
    assert capture.active_capture.sink_name == "mena_session_1_remote"
    assert capture.start() == ()


def test_worker_capture_session_rejects_missing_required_route(tmp_path: Path) -> None:
    payload = StartCapturePayload(
        profile="online_dual_stream",
        language="en",
        segment_seconds=30,
        selected_sources=("local_mic", "system_audio"),
        routes=_start_payload().routes[:2],
    )
    capture = WorkerCaptureSession.create_deterministic(
        session_id="session-1",
        session_dir=tmp_path,
        start_payload=payload,
    )

    with pytest.raises(ProtocolError, match="missing output_sink route"):
        capture.start()


def test_segment_flush_loop_emits_status_then_structured_error() -> None:
    class FlakyCapture:
        def __init__(self) -> None:
            self.segments: list[CaptureSegment] = []
            self.calls = 0
            self.aborted = False

        def flush_once(
            self,
            *,
            stop_event: threading.Event | None = None,
        ) -> tuple[CaptureSegment, ...]:
            self.calls += 1
            if self.calls == 1:
                segment = _capture_segment(source_id=CaptureSourceId.LOCAL_MIC, index=1)
                self.segments.append(segment)
                return (segment,)
            raise RuntimeError("disk full")

        def abort(self) -> None:
            self.aborted = True

    class AbortCheckingOutput(BytesIO):
        def write(self, data: Any) -> int:
            if b"capture_flush_failed" in data:
                assert capture.aborted
            return super().write(data)

    capture = FlakyCapture()
    output = AbortCheckingOutput()
    loop = SegmentFlushLoop(
        capture=cast(WorkerCaptureSession, capture),
        session_id="session-1",
        protocol_out=output,
        write_lock=threading.Lock(),
    )

    loop.start()
    loop.join()

    frames = [ProtocolMessage.decode(frame) for frame in output.getvalue().splitlines(True)]
    assert [frame.type for frame in frames] == ["segments_flushed", "capture_status", "error"]
    assert frames[1].payload["sealed_segment_count"] == 1
    assert ErrorPayload.from_dict(frames[2].payload).message == "disk full"
    assert capture.aborted
    assert isinstance(loop.error, RuntimeError)


def test_segment_flush_loop_bounds_large_error_message() -> None:
    class FlakyCapture:
        segments: list[CaptureSegment] = []
        aborted = False

        def flush_once(
            self,
            *,
            stop_event: threading.Event | None = None,
        ) -> tuple[CaptureSegment, ...]:
            raise RuntimeError("x" * (MAX_FRAME_BYTES * 2))

        def abort(self) -> None:
            self.aborted = True

    capture = FlakyCapture()
    output = BytesIO()
    loop = SegmentFlushLoop(
        capture=cast(WorkerCaptureSession, capture),
        session_id="session-1",
        protocol_out=output,
        write_lock=threading.Lock(),
    )

    loop.start()
    loop.join()

    frame = ProtocolMessage.decode(output.getvalue())
    error = ErrorPayload.from_dict(frame.payload)
    assert frame.type == "error"
    assert error.code == "capture_flush_failed"
    assert error.message.endswith("below 65536 bytes]")
    assert len(output.getvalue()) <= MAX_FRAME_BYTES
    assert capture.aborted


def test_segment_flush_loop_emits_multiple_iterations_until_stopped() -> None:
    class CountingCapture:
        def __init__(self) -> None:
            self.segments: list[CaptureSegment] = []
            self.calls = 0
            self.ready_to_stop = threading.Event()

        def flush_once(
            self,
            *,
            stop_event: threading.Event | None = None,
        ) -> tuple[CaptureSegment, ...]:
            self.calls += 1
            segment = _capture_segment(source_id=CaptureSourceId.LOCAL_MIC, index=self.calls)
            self.segments.append(segment)
            if self.calls >= 3:
                self.ready_to_stop.set()
                while stop_event is not None and not stop_event.is_set():
                    time.sleep(0.01)
            return (segment,)

    capture = CountingCapture()
    output = BytesIO()
    loop = SegmentFlushLoop(
        capture=cast(WorkerCaptureSession, capture),
        session_id="session-1",
        protocol_out=output,
        write_lock=threading.Lock(),
    )

    loop.start()
    assert capture.ready_to_stop.wait(timeout=1.0)
    loop.stop()
    loop.join()

    frames = [ProtocolMessage.decode(frame) for frame in output.getvalue().splitlines(True)]
    flushed_indexes = [
        segment.index
        for frame in frames
        if frame.type == "segments_flushed"
        for segment in SegmentsFlushedPayload.from_dict(frame.payload).segments
    ]
    status_counts = [
        CaptureStatusPayload.from_dict(frame.payload).sealed_segment_count
        for frame in frames
        if frame.type == "capture_status"
    ]
    assert flushed_indexes == [1, 2, 3]
    assert status_counts[-1] == 3


def test_worker_run_loop_handles_in_person_room_mic_capture(tmp_path: Path) -> None:
    stdin = BytesIO(
        b"".join(
            [
                hello_message("session-1").encode(),
                ProtocolMessage(
                    type="start_capture",
                    session_id="session-1",
                    payload=_in_person_start_payload().to_dict(),
                ).encode(),
                ProtocolMessage(type="stop_capture", session_id="session-1").encode(),
            ]
        )
    )
    stdout = BytesIO()

    exit_code = run_worker(
        session_id="session-1",
        session_dir=tmp_path,
        stdin=stdin,
        stdout=stdout,
        test_deterministic_capture=True,
    )

    frames = [ProtocolMessage.decode(frame) for frame in stdout.getvalue().splitlines(True)]
    finalized = CaptureFinalizedPayload.from_dict(frames[-1].payload)
    assert exit_code == 0
    assert finalized.capture_metadata["profile"] == "in_person_single_mic"
    sources = finalized.capture_metadata["sources"]
    assert "segments" not in finalized.capture_metadata
    assert isinstance(sources, list)
    assert sources[0]["route_id"] == "pipewire:node:1"
    assert sources[0]["route_label"] == "Room microphone"


def test_worker_run_loop_stops_on_orchestrator_eof(tmp_path: Path) -> None:
    stdin = BytesIO(hello_message("session-1").encode())
    stdout = BytesIO()
    stderr = StringIO()

    exit_code = run_worker(
        session_id="session-1",
        session_dir=tmp_path,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert ProtocolMessage.decode(stdout.getvalue()).type == "hello_ack"
    assert "orchestrator closed protocol stdin" in stderr.getvalue()


def test_worker_run_loop_handles_eof_before_hello(tmp_path: Path) -> None:
    stdout = BytesIO()
    stderr = StringIO()

    exit_code = run_worker(
        session_id="session-1",
        session_dir=tmp_path,
        stdin=BytesIO(),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert stdout.getvalue() == b""
    assert "before hello" in stderr.getvalue()


def test_worker_run_loop_rejects_unexpected_command(tmp_path: Path) -> None:
    stdin = BytesIO(
        hello_message("session-1").encode()
        + ProtocolMessage(
            type="capture_started",
            session_id="session-1",
            payload={
                "session_dir": str(tmp_path),
                "profile": "online_dual_stream",
                "segment_seconds": 30,
                "selected_sources": ["local_mic", "system_audio"],
            },
        ).encode()
    )

    with pytest.raises(ProtocolError, match="Unexpected worker command 'capture_started'"):
        run_worker(session_id="session-1", session_dir=tmp_path, stdin=stdin, stdout=BytesIO())


def test_worker_run_loop_rejects_mismatched_session_id(tmp_path: Path) -> None:
    stdin = BytesIO(
        hello_message("session-1").encode()
        + ProtocolMessage(
            type="start_capture",
            session_id="session-2",
            payload=_start_payload().to_dict(),
        ).encode()
    )

    with pytest.raises(ProtocolError, match="session_id does not match"):
        run_worker(session_id="session-1", session_dir=tmp_path, stdin=stdin, stdout=BytesIO())


def test_worker_run_loop_rejects_mismatched_hello_session_id(tmp_path: Path) -> None:
    stdin = BytesIO(hello_message("session-2").encode())

    with pytest.raises(ProtocolError, match="Hello session_id does not match"):
        run_worker(session_id="session-1", session_dir=tmp_path, stdin=stdin, stdout=BytesIO())


def test_worker_run_loop_rejects_duplicate_start(tmp_path: Path) -> None:
    stdin = BytesIO(
        hello_message("session-1").encode()
        + ProtocolMessage(
            type="start_capture",
            session_id="session-1",
            payload=_start_payload().to_dict(),
        ).encode()
        + ProtocolMessage(
            type="start_capture",
            session_id="session-1",
            payload=_start_payload().to_dict(),
        ).encode()
    )

    with pytest.raises(ProtocolError, match="already started"):
        run_worker(
            session_id="session-1",
            session_dir=tmp_path,
            stdin=stdin,
            stdout=BytesIO(),
            test_deterministic_capture=True,
        )


def test_worker_run_loop_rejects_stop_before_start(tmp_path: Path) -> None:
    stdin = BytesIO(
        hello_message("session-1").encode()
        + ProtocolMessage(type="stop_capture", session_id="session-1").encode()
    )

    with pytest.raises(ProtocolError, match="Cannot stop capture before it has started"):
        run_worker(session_id="session-1", session_dir=tmp_path, stdin=stdin, stdout=BytesIO())


def test_worker_process_context_terminates_running_child(tmp_path: Path) -> None:
    with WorkerProcess(
        session_id="session-1",
        session_dir=tmp_path,
        argv=[sys.executable, "-c", "import time; time.sleep(60)"],
    ) as worker:
        process = worker.process

    assert process.poll() is not None


def test_worker_process_stop_raises_structured_worker_error(tmp_path: Path) -> None:
    with (
        WorkerProcess(
            session_id="session-1",
            session_dir=tmp_path,
            argv=[
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "sys.stdin.readline(); "
                    "sys.stdout.write("
                    '\'{"v":1,"type":"error","id":"message-1",'
                    '"session_id":"session-1","payload":'
                    '{"code":"capture_failed","message":"boom",'
                    '"recoverable":false,"details":{}}}\\n\''
                    "); "
                    "sys.stdout.flush()"
                ),
            ],
        ) as worker,
        pytest.raises(ProtocolError, match="boom"),
    ):
        worker.stop_capture()


def test_worker_process_redirects_stderr_to_session_log(tmp_path: Path) -> None:
    with (
        WorkerProcess(
            session_id="session-1",
            session_dir=tmp_path,
            argv=[
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('worker diagnostic\\n'); raise SystemExit(3)",
            ],
        ) as worker,
        pytest.raises(WorkerExitedError, match="worker diagnostic"),
    ):
        assert worker.process.stderr is None
        worker.handshake()


def test_worker_hard_kill_preserves_sealed_prior_segments(tmp_path: Path) -> None:
    with WorkerProcess(
        session_id="session-1",
        session_dir=tmp_path,
        argv=_deterministic_worker_argv(session_id="session-1", session_dir=tmp_path),
    ) as worker:
        worker.handshake()
        worker.start_capture(_start_payload())
        worker.receive_expected("segments_flushed")
        worker.process.kill()
        worker.wait(timeout=2)

    local_segment = tmp_path / "artifacts" / "source" / "local_mic" / "local_mic_000001.wav"
    remote_segment = tmp_path / "artifacts" / "source" / "system_audio" / "system_audio_000001.wav"
    assert local_segment.exists()
    assert remote_segment.exists()
    with wave.open(str(local_segment), "rb") as wav:
        assert wav.getframerate() == 48_000
        assert wav.getnframes() == 1_440_000


def test_worker_eof_after_start_finalizes_existing_segments(tmp_path: Path) -> None:
    stdin = BytesIO(
        hello_message("session-1").encode()
        + ProtocolMessage(
            type="start_capture",
            session_id="session-1",
            payload=_start_payload().to_dict(),
        ).encode()
    )
    stdout = BytesIO()
    stderr = StringIO()

    exit_code = run_worker(
        session_id="session-1",
        session_dir=tmp_path,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        test_deterministic_capture=True,
    )

    assert exit_code == 0
    assert "orchestrator closed protocol stdin" in stderr.getvalue()
    assert (tmp_path / "artifacts" / "source" / "local_mic" / "local_mic_000001.wav").exists()


def test_worker_version_mismatch_exits_with_diagnostics_only_on_stderr(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mena.capture_worker",
            "--session-id",
            "session-1",
            "--session-dir",
            str(tmp_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    process.stdin.write(
        ProtocolMessage(
            type="hello",
            session_id="session-1",
            payload={"protocol_major": 99, "protocol_minor": 0, "role": "worker"},
        ).encode()
    )
    process.stdin.close()

    assert process.wait(timeout=2) == 2
    assert process.stdout.read() == b""
    assert b"protocol error:" in process.stderr.read()


def test_worker_process_detects_child_exit_before_event(tmp_path: Path) -> None:
    with (
        WorkerProcess(
            session_id="session-1",
            session_dir=tmp_path,
            argv=[sys.executable, "-c", "raise SystemExit(3)"],
        ) as worker,
        pytest.raises(WorkerExitedError, match="Worker exited"),
    ):
        worker.handshake()


def _start_payload() -> StartCapturePayload:
    return StartCapturePayload(
        profile="online_dual_stream",
        language="en",
        segment_seconds=30,
        selected_sources=("local_mic", "system_audio"),
        routes=(
            CaptureRouteBindingPayload(
                role="microphone",
                source_id="local_mic",
                route_id="pipewire:node:1",
                label="Headset microphone",
                backend="pipewire",
                required=True,
                backend_name="alsa_input.usb-headset",
                backend_target="alsa_input.usb-headset",
                node_name="alsa_input.usb-headset",
                object_serial="101",
            ),
            CaptureRouteBindingPayload(
                role="playback_stream",
                source_id="system_audio",
                route_id="pulse:sink-input:2",
                label="Meeting playback",
                backend="pulse",
                required=True,
                backend_name="meeting-playback",
                backend_target="2",
                sink_input_id="2",
            ),
            CaptureRouteBindingPayload(
                role="output_sink",
                source_id=None,
                route_id="pipewire:node:3",
                label="Headphones",
                backend="pipewire",
                required=True,
                backend_name="alsa_output.usb-headphones",
                backend_target="alsa_output.usb-headphones",
                node_name="alsa_output.usb-headphones",
                object_serial="103",
            ),
        ),
    )


def _in_person_start_payload() -> StartCapturePayload:
    return StartCapturePayload(
        profile="in_person_single_mic",
        language="de-CH",
        segment_seconds=30,
        selected_sources=("room_mic",),
        routes=(
            CaptureRouteBindingPayload(
                role="microphone",
                source_id="room_mic",
                route_id="pipewire:node:1",
                label="Room microphone",
                backend="pipewire",
                required=True,
                backend_name="alsa_input.room",
                backend_target="alsa_input.room",
                node_name="alsa_input.room",
                object_serial="101",
            ),
        ),
    )


def _deterministic_worker_argv(*, session_id: str, session_dir: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "mena.capture_worker",
        "--session-id",
        session_id,
        "--session-dir",
        str(session_dir),
        "--test-deterministic-capture",
    ]


def _segment_payload(*, source_id: str) -> dict[str, object]:
    return _capture_segment(source_id=CaptureSourceId(source_id), index=1).to_dict()


def _capture_segment(*, source_id: CaptureSourceId, index: int) -> CaptureSegment:
    return CaptureSegment(
        source_id=source_id,
        index=index,
        path=Path(f"artifacts/source/{source_id.value}/{source_id.value}_{index:06d}.wav"),
        sample_rate=48_000,
        channels=1,
        channel_layout="mono",
        sample_format="pcm_s16le",
        byte_count=44,
        duration_seconds=30.0,
        started_at=None,
        ended_at=None,
        sha256="0" * 64,
    )


def _capture_metadata() -> dict[str, object]:
    segment = _segment_payload(source_id="local_mic")
    return {
        "version": 1,
        "profile": "online_dual_stream",
        "sample_rate": 48_000,
        "channels": 1,
        "segment_seconds": 30,
        "segment_count": 1,
        "segments": [segment],
        "sources": [
            {
                "source_id": "local_mic",
                "route_id": "pipewire:node:1",
                "route_label": "Headset microphone",
                "capture_mode": "selected_microphone",
                "channel_layout": "mono",
                "segment_count": 1,
            }
        ],
    }
