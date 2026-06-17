"""Internal stdio JSON-L protocol for orchestrator and capture worker."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import IO, Self
from uuid import uuid4

PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 1
ENVELOPE_VERSION = 1
MAX_FRAME_BYTES = 64 * 1024

CAPTURE_PROFILE_IDS = frozenset({"online_dual_stream", "in_person_single_mic"})
CAPTURE_SOURCE_IDS = frozenset({"local_mic", "system_audio", "room_mic"})
CAPTURE_ROUTE_ROLES = frozenset({"microphone", "playback_stream", "output_sink"})
CAPTURE_STATUS_VALUES = frozenset({"recording", "finalizing"})
CAPTURE_FINALIZED_STATUS_VALUES = frozenset({"recorded", "interrupted_partial", "failed"})
CAPTURE_STOP_REASONS = frozenset({"user_stop", "orchestrator_eof", "interrupted", "error"})
SEGMENT_STATUS_VALUES = frozenset({"sealed", "partial", "failed"})
CAPTURE_ROUTE_EVENT_TYPES = frozenset({"degraded", "recovered"})
CAPTURE_MODE_VALUES = frozenset({"selected_microphone", "dedicated_route", "system_mix_remote"})

MESSAGE_TYPES = frozenset(
    {
        "hello",
        "hello_ack",
        "start_capture",
        "stop_capture",
        "capture_started",
        "capture_status",
        "capture_route_event",
        "segment_flushed",
        "segments_flushed",
        "capture_finalized",
        "error",
    }
)


class ProtocolError(RuntimeError):
    """Raised when a peer sends an invalid protocol frame."""


class WorkerExitedError(RuntimeError):
    """Raised when the worker exits before producing the expected event."""


@dataclass(frozen=True)
class ProtocolMessage:
    """One newline-delimited protocol message."""

    type: str
    session_id: str
    payload: dict[str, object] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid4()))
    v: int = ENVELOPE_VERSION

    def encode(self) -> bytes:
        if self.v != ENVELOPE_VERSION:
            raise ProtocolError(
                f"Unsupported protocol envelope version {self.v}; expected {ENVELOPE_VERSION}."
            )
        validate_message_type(self.type)
        if not isinstance(self.id, str) or not self.id:
            raise ProtocolError("Protocol id must be a non-empty string.")
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ProtocolError("Protocol session_id must be a non-empty string.")
        if not isinstance(self.payload, dict):
            raise ProtocolError("Protocol payload must be a JSON object.")
        validate_payload_for_type(self.type, self.payload)
        frame = {
            "v": self.v,
            "type": self.type,
            "id": self.id,
            "session_id": self.session_id,
            "payload": self.payload,
        }
        try:
            encoded = (
                json.dumps(frame, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n"
            )
        except TypeError as exc:
            raise ProtocolError("Protocol payload must be JSON serializable.") from exc
        if len(encoded) > MAX_FRAME_BYTES:
            raise ProtocolError(f"Protocol frame exceeds {MAX_FRAME_BYTES} bytes.")
        return encoded

    @classmethod
    def decode(cls, line: bytes) -> Self:
        if len(line) > MAX_FRAME_BYTES:
            raise ProtocolError(f"Protocol frame exceeds {MAX_FRAME_BYTES} bytes.")
        if not line.endswith(b"\n"):
            raise ProtocolError("Protocol frame is not LF terminated.")
        try:
            data = json.loads(line.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise ProtocolError("Protocol frame must be valid UTF-8.") from exc
        except json.JSONDecodeError as exc:
            raise ProtocolError("Protocol frame must be valid JSON.") from exc
        if not isinstance(data, dict):
            raise ProtocolError("Protocol frame must be a JSON object.")

        version = _required_int(data, "v")
        if version != ENVELOPE_VERSION:
            raise ProtocolError(
                f"Unsupported protocol envelope version {version}; expected {ENVELOPE_VERSION}."
            )
        message_type = _required_str(data, "type")
        validate_message_type(message_type)
        payload = data.get("payload")
        if not isinstance(payload, dict):
            raise ProtocolError("Protocol payload must be a JSON object.")
        validate_payload_for_type(message_type, payload)

        return cls(
            v=version,
            type=message_type,
            id=_required_str(data, "id"),
            session_id=_required_str(data, "session_id"),
            payload=payload,
        )


def validate_message_type(message_type: str) -> None:
    if message_type not in MESSAGE_TYPES:
        valid_types = ", ".join(sorted(MESSAGE_TYPES))
        raise ProtocolError(
            f"Unknown protocol message type '{message_type}'. Valid types: {valid_types}."
        )


@dataclass(frozen=True)
class CaptureRouteBindingPayload:
    """One concrete route binding inside a start-capture request."""

    role: str
    source_id: str | None
    route_id: str
    label: str
    backend: str
    required: bool
    backend_name: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "source_id": self.source_id,
            "route_id": self.route_id,
            "label": self.label,
            "backend": self.backend,
            "backend_name": self.backend_name,
            "required": self.required,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        payload = _ensure_mapping(data, "capture route binding")
        _require_exact_keys(
            payload,
            {"role", "source_id", "route_id", "label", "backend", "backend_name", "required"},
            "capture route binding",
        )
        source_id = _optional_enum_str(
            payload,
            "source_id",
            CAPTURE_SOURCE_IDS,
            "capture route binding",
        )
        return cls(
            role=_required_enum_str(
                payload,
                "role",
                CAPTURE_ROUTE_ROLES,
                "capture route binding",
            ),
            source_id=source_id,
            route_id=_required_str(payload, "route_id"),
            label=_required_str(payload, "label"),
            backend=_required_str(payload, "backend"),
            backend_name=_optional_str(payload, "backend_name", "capture route binding"),
            required=_required_bool(payload, "required"),
        )


@dataclass(frozen=True)
class StartCapturePayload:
    """Strict route plan sent by the orchestrator before capture begins."""

    profile: str
    language: str
    segment_seconds: int
    selected_sources: tuple[str, ...]
    routes: tuple[CaptureRouteBindingPayload, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "language": self.language,
            "segment_seconds": self.segment_seconds,
            "selected_sources": list(self.selected_sources),
            "routes": [route.to_dict() for route in self.routes],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        payload = _ensure_mapping(data, "start_capture payload")
        _require_exact_keys(
            payload,
            {"profile", "language", "segment_seconds", "selected_sources", "routes"},
            "start_capture payload",
        )
        selected_sources = _required_str_sequence(
            payload,
            "selected_sources",
            "start_capture payload",
        )
        if not selected_sources:
            raise ProtocolError("start_capture payload selected_sources must not be empty.")
        invalid_sources = sorted(set(selected_sources) - CAPTURE_SOURCE_IDS)
        if invalid_sources:
            raise ProtocolError(
                "start_capture payload selected_sources contains unsupported source "
                f"'{invalid_sources[0]}'."
            )
        if len(set(selected_sources)) != len(selected_sources):
            raise ProtocolError("start_capture payload selected_sources must be unique.")
        raw_routes = _required_list(payload, "routes", "start_capture payload")
        parsed_routes: list[CaptureRouteBindingPayload] = []
        for route in raw_routes:
            if not isinstance(route, dict):
                raise ProtocolError("start_capture payload routes must contain JSON objects.")
            parsed_routes.append(CaptureRouteBindingPayload.from_dict(route))
        routes = tuple(parsed_routes)
        if not routes:
            raise ProtocolError("start_capture payload routes must not be empty.")
        return cls(
            profile=_required_enum_str(
                payload,
                "profile",
                CAPTURE_PROFILE_IDS,
                "start_capture payload",
            ),
            language=_required_str(payload, "language"),
            segment_seconds=_required_positive_int(payload, "segment_seconds"),
            selected_sources=selected_sources,
            routes=routes,
        )


@dataclass(frozen=True)
class CaptureStartedPayload:
    """Worker event confirming capture startup."""

    session_dir: str
    profile: str
    segment_seconds: int
    selected_sources: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "session_dir": self.session_dir,
            "profile": self.profile,
            "segment_seconds": self.segment_seconds,
            "selected_sources": list(self.selected_sources),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        payload = _ensure_mapping(data, "capture_started payload")
        _require_exact_keys(
            payload,
            {"session_dir", "profile", "segment_seconds", "selected_sources"},
            "capture_started payload",
        )
        selected_sources = _required_str_sequence(
            payload,
            "selected_sources",
            "capture_started payload",
        )
        if not selected_sources:
            raise ProtocolError("capture_started payload selected_sources must not be empty.")
        invalid_sources = sorted(set(selected_sources) - CAPTURE_SOURCE_IDS)
        if invalid_sources:
            raise ProtocolError(
                "capture_started payload selected_sources contains unsupported source "
                f"'{invalid_sources[0]}'."
            )
        return cls(
            session_dir=_required_str(payload, "session_dir"),
            profile=_required_enum_str(
                payload,
                "profile",
                CAPTURE_PROFILE_IDS,
                "capture_started payload",
            ),
            segment_seconds=_required_positive_int(payload, "segment_seconds"),
            selected_sources=selected_sources,
        )


@dataclass(frozen=True)
class CaptureStatusPayload:
    """Worker heartbeat while capture is active."""

    status: str
    elapsed_seconds: float
    sealed_segment_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "elapsed_seconds": self.elapsed_seconds,
            "sealed_segment_count": self.sealed_segment_count,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        payload = _ensure_mapping(data, "capture_status payload")
        _require_exact_keys(
            payload,
            {"status", "elapsed_seconds", "sealed_segment_count"},
            "capture_status payload",
        )
        return cls(
            status=_required_enum_str(
                payload,
                "status",
                CAPTURE_STATUS_VALUES,
                "capture_status payload",
            ),
            elapsed_seconds=_required_non_negative_number(payload, "elapsed_seconds"),
            sealed_segment_count=_required_non_negative_int(payload, "sealed_segment_count"),
        )


@dataclass(frozen=True)
class CaptureRouteEventPayload:
    """Route-mode degradation or recovery event emitted by the worker."""

    event: str
    code: str
    message: str
    source_id: str | None
    occurred_at: str | None
    elapsed_seconds: float | None
    segment_index: int | None
    route_id: str | None
    route_label: str | None
    capture_mode: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "event": self.event,
            "code": self.code,
            "message": self.message,
            "source_id": self.source_id,
            "occurred_at": self.occurred_at,
            "elapsed_seconds": self.elapsed_seconds,
            "segment_index": self.segment_index,
            "route_id": self.route_id,
            "route_label": self.route_label,
            "capture_mode": self.capture_mode,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        payload = _ensure_mapping(data, "capture_route_event payload")
        _require_exact_keys(
            payload,
            {
                "event",
                "code",
                "message",
                "source_id",
                "occurred_at",
                "elapsed_seconds",
                "segment_index",
                "route_id",
                "route_label",
                "capture_mode",
            },
            "capture_route_event payload",
        )
        return cls(
            event=_required_enum_str(
                payload,
                "event",
                CAPTURE_ROUTE_EVENT_TYPES,
                "capture_route_event payload",
            ),
            code=_required_str(payload, "code"),
            message=_required_str(payload, "message"),
            source_id=_optional_enum_str(
                payload,
                "source_id",
                CAPTURE_SOURCE_IDS,
                "capture_route_event payload",
            ),
            occurred_at=_optional_str(payload, "occurred_at", "capture_route_event payload"),
            elapsed_seconds=_optional_non_negative_number(
                payload,
                "elapsed_seconds",
                "capture_route_event payload",
            ),
            segment_index=_optional_positive_int(
                payload,
                "segment_index",
                "capture_route_event payload",
            ),
            route_id=_optional_str(payload, "route_id", "capture_route_event payload"),
            route_label=_optional_str(payload, "route_label", "capture_route_event payload"),
            capture_mode=_optional_enum_str(
                payload,
                "capture_mode",
                CAPTURE_MODE_VALUES,
                "capture_route_event payload",
            ),
        )


@dataclass(frozen=True)
class SegmentFlushedPayload:
    """Worker event for one sealed source segment."""

    source_id: str
    index: int
    path: str
    sample_rate: int
    channels: int
    channel_layout: str
    sample_format: str
    byte_count: int
    duration_seconds: float
    started_at: str | None
    ended_at: str | None
    sha256: str
    route_id: str | None
    route_label: str | None
    capture_mode: str | None
    status: str
    format: str

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        payload = _ensure_mapping(data, "segment_flushed payload")
        _require_exact_keys(
            payload,
            {
                "source_id",
                "index",
                "path",
                "sample_rate",
                "channels",
                "channel_layout",
                "sample_format",
                "byte_count",
                "duration_seconds",
                "started_at",
                "ended_at",
                "sha256",
                "route_id",
                "route_label",
                "capture_mode",
                "status",
                "format",
            },
            "segment_flushed payload",
        )
        segment_format = _required_str(payload, "format")
        if segment_format != "wav":
            raise ProtocolError("segment_flushed payload format must be 'wav'.")
        return cls(
            source_id=_required_enum_str(
                payload,
                "source_id",
                CAPTURE_SOURCE_IDS,
                "segment_flushed payload",
            ),
            index=_required_positive_int(payload, "index"),
            path=_required_str(payload, "path"),
            sample_rate=_required_positive_int(payload, "sample_rate"),
            channels=_required_positive_int(payload, "channels"),
            channel_layout=_required_str(payload, "channel_layout"),
            sample_format=_required_str(payload, "sample_format"),
            byte_count=_required_non_negative_int(payload, "byte_count"),
            duration_seconds=_required_non_negative_number(payload, "duration_seconds"),
            started_at=_optional_str(payload, "started_at", "segment_flushed payload"),
            ended_at=_optional_str(payload, "ended_at", "segment_flushed payload"),
            sha256=_required_sha256(payload, "sha256"),
            route_id=_optional_str(payload, "route_id", "segment_flushed payload"),
            route_label=_optional_str(payload, "route_label", "segment_flushed payload"),
            capture_mode=_optional_enum_str(
                payload,
                "capture_mode",
                CAPTURE_MODE_VALUES,
                "segment_flushed payload",
            ),
            status=_required_enum_str(
                payload,
                "status",
                SEGMENT_STATUS_VALUES,
                "segment_flushed payload",
            ),
            format=segment_format,
        )


@dataclass(frozen=True)
class SegmentsFlushedPayload:
    """Worker event for one aligned flush containing one or more source segments."""

    segments: tuple[SegmentFlushedPayload, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "segments": [
                {
                    "source_id": segment.source_id,
                    "index": segment.index,
                    "path": segment.path,
                    "sample_rate": segment.sample_rate,
                    "channels": segment.channels,
                    "channel_layout": segment.channel_layout,
                    "sample_format": segment.sample_format,
                    "byte_count": segment.byte_count,
                    "duration_seconds": segment.duration_seconds,
                    "started_at": segment.started_at,
                    "ended_at": segment.ended_at,
                    "sha256": segment.sha256,
                    "route_id": segment.route_id,
                    "route_label": segment.route_label,
                    "capture_mode": segment.capture_mode,
                    "status": segment.status,
                    "format": segment.format,
                }
                for segment in self.segments
            ]
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        payload = _ensure_mapping(data, "segments_flushed payload")
        _require_exact_keys(payload, {"segments"}, "segments_flushed payload")
        raw_segments = _required_list(payload, "segments", "segments_flushed payload")
        if not raw_segments:
            raise ProtocolError("segments_flushed payload segments must not be empty.")
        segments: list[SegmentFlushedPayload] = []
        for segment in raw_segments:
            if not isinstance(segment, dict):
                raise ProtocolError("segments_flushed payload segments must contain JSON objects.")
            segments.append(SegmentFlushedPayload.from_dict(segment))
        return cls(segments=tuple(segments))


@dataclass(frozen=True)
class CaptureFinalizedPayload:
    """Worker event for normal or partial capture finalization."""

    status: str
    stop_reason: str
    completed_at: str | None
    error: str | None
    capture_metadata: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "stop_reason": self.stop_reason,
            "completed_at": self.completed_at,
            "error": self.error,
            "capture_metadata": self.capture_metadata,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        payload = _ensure_mapping(data, "capture_finalized payload")
        _require_exact_keys(
            payload,
            {"status", "stop_reason", "completed_at", "error", "capture_metadata"},
            "capture_finalized payload",
        )
        capture_metadata = _required_mapping(
            payload,
            "capture_metadata",
            "capture_finalized payload",
        )
        _validate_capture_metadata_or_summary(capture_metadata)
        return cls(
            status=_required_enum_str(
                payload,
                "status",
                CAPTURE_FINALIZED_STATUS_VALUES,
                "capture_finalized payload",
            ),
            stop_reason=_required_enum_str(
                payload,
                "stop_reason",
                CAPTURE_STOP_REASONS,
                "capture_finalized payload",
            ),
            completed_at=_optional_str(payload, "completed_at", "capture_finalized payload"),
            error=_optional_str(payload, "error", "capture_finalized payload"),
            capture_metadata=dict(capture_metadata),
        )


@dataclass(frozen=True)
class ErrorPayload:
    """Structured protocol error event."""

    code: str
    message: str
    recoverable: bool
    details: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "recoverable": self.recoverable,
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Self:
        payload = _ensure_mapping(data, "error payload")
        _require_exact_keys(payload, {"code", "message", "recoverable", "details"}, "error payload")
        details = _required_mapping(payload, "details", "error payload")
        return cls(
            code=_required_str(payload, "code"),
            message=_required_str(payload, "message"),
            recoverable=_required_bool(payload, "recoverable"),
            details=dict(details),
        )


def validate_payload_for_type(message_type: str, payload: Mapping[str, object]) -> None:
    """Validate typed payloads for #83 protocol messages."""

    if message_type == "start_capture":
        StartCapturePayload.from_dict(payload)
    elif message_type == "capture_started":
        CaptureStartedPayload.from_dict(payload)
    elif message_type == "capture_status":
        CaptureStatusPayload.from_dict(payload)
    elif message_type == "capture_route_event":
        CaptureRouteEventPayload.from_dict(payload)
    elif message_type == "segment_flushed":
        SegmentFlushedPayload.from_dict(payload)
    elif message_type == "segments_flushed":
        SegmentsFlushedPayload.from_dict(payload)
    elif message_type == "capture_finalized":
        CaptureFinalizedPayload.from_dict(payload)
    elif message_type == "error":
        ErrorPayload.from_dict(payload)


def read_message(stream: IO[bytes]) -> ProtocolMessage:
    line = stream.readline(MAX_FRAME_BYTES + 1)
    if line == b"":
        raise EOFError("Protocol peer closed the stream.")
    return ProtocolMessage.decode(line)


def write_message(stream: IO[bytes], message: ProtocolMessage) -> None:
    stream.write(message.encode())
    stream.flush()


def hello_message(session_id: str) -> ProtocolMessage:
    return ProtocolMessage(
        type="hello",
        session_id=session_id,
        payload={
            "protocol_major": PROTOCOL_MAJOR,
            "protocol_minor": PROTOCOL_MINOR,
            "role": "worker",
        },
    )


def hello_ack_message(session_id: str, request_id: str) -> ProtocolMessage:
    return ProtocolMessage(
        type="hello_ack",
        id=request_id,
        session_id=session_id,
        payload={
            "accepted": True,
            "protocol_major": PROTOCOL_MAJOR,
            "protocol_minor": PROTOCOL_MINOR,
        },
    )


def validate_hello(message: ProtocolMessage) -> None:
    if message.type != "hello":
        raise ProtocolError("First protocol message must be hello.")
    protocol_major = message.payload.get("protocol_major")
    if protocol_major != PROTOCOL_MAJOR:
        raise ProtocolError(
            f"Unsupported protocol major version {protocol_major}; expected {PROTOCOL_MAJOR}."
        )
    _required_int(message.payload, "protocol_minor")
    if message.payload.get("role") != "worker":
        raise ProtocolError("Hello role must be 'worker'.")


class WorkerProcess:
    """Orchestrator-side handle for a capture worker subprocess."""

    def __init__(
        self,
        *,
        session_id: str,
        session_dir: Path,
        argv: list[str] | None = None,
    ) -> None:
        self.session_id = session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        self._stderr_path = session_dir / "worker.stderr.log"
        self._stderr_stream = self._stderr_path.open("w+b")
        command = argv or [
            sys.executable,
            "-m",
            "mena.capture_worker",
            "--session-id",
            session_id,
            "--session-dir",
            str(session_dir),
        ]
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr_stream,
                shell=False,
            )
        except Exception:
            self._stderr_stream.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.process.poll() is None:
            self.terminate()
        self._stderr_stream.close()

    def handshake(self) -> ProtocolMessage:
        hello = hello_message(self.session_id)
        self.send(hello)
        response = self.receive()
        if response.type != "hello_ack":
            raise ProtocolError(f"Expected hello_ack, got {response.type}.")
        if response.id != hello.id:
            raise ProtocolError("hello_ack id does not match hello id.")
        if response.session_id != self.session_id:
            raise ProtocolError("hello_ack session_id does not match worker session_id.")
        if response.payload.get("accepted") is not True:
            raise ProtocolError("Worker rejected protocol handshake.")
        return response

    def start_capture(
        self,
        payload: StartCapturePayload | Mapping[str, object],
    ) -> ProtocolMessage:
        payload_dict = (
            payload.to_dict() if isinstance(payload, StartCapturePayload) else dict(payload)
        )
        self.send(
            ProtocolMessage(type="start_capture", session_id=self.session_id, payload=payload_dict)
        )
        return self.receive_expected("capture_started")

    def stop_capture(self) -> ProtocolMessage:
        self.request_stop_capture()
        while True:
            message = self.receive()
            if message.type == "capture_finalized":
                return message
            if message.type == "error":
                error = ErrorPayload.from_dict(message.payload)
                raise ProtocolError(error.message)

    def request_stop_capture(self) -> None:
        self.send(ProtocolMessage(type="stop_capture", session_id=self.session_id))

    def send(self, message: ProtocolMessage) -> None:
        if self.process.stdin is None:
            raise WorkerExitedError("Worker stdin is not available.")
        try:
            write_message(self.process.stdin, message)
        except BrokenPipeError as exc:
            raise WorkerExitedError("Worker exited before accepting protocol input.") from exc

    def receive(self) -> ProtocolMessage:
        if self.process.stdout is None:
            raise WorkerExitedError("Worker stdout is not available.")
        try:
            return read_message(self.process.stdout)
        except EOFError as exc:
            stderr = self.read_stderr()
            detail = f" Worker stderr: {stderr}" if stderr else ""
            raise WorkerExitedError(
                f"Worker exited before sending a protocol frame.{detail}"
            ) from exc

    def receive_expected(self, message_type: str) -> ProtocolMessage:
        message = self.receive()
        if message.type != message_type:
            raise ProtocolError(f"Expected {message_type}, got {message.type}.")
        return message

    def terminate(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)

    def wait(self, timeout: float | None = None) -> int:
        return self.process.wait(timeout=timeout)

    def read_stderr(self) -> str:
        if self._stderr_stream.closed:
            return self._stderr_path.read_text(encoding="utf-8", errors="replace")
        self._stderr_stream.flush()
        self._stderr_stream.seek(0)
        stderr = self._stderr_stream.read().decode("utf-8", errors="replace")
        self._stderr_stream.seek(0, 2)
        return stderr


def _required_str(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"Protocol field '{key}' must be a non-empty string.")
    return value


def _required_int(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    if type(value) is not int:
        raise ProtocolError(f"Protocol field '{key}' must be an integer.")
    return value


def _required_positive_int(data: dict[str, object], key: str) -> int:
    value = _required_int(data, key)
    if value < 1:
        raise ProtocolError(f"Protocol field '{key}' must be a positive integer.")
    return value


def _required_non_negative_int(data: dict[str, object], key: str) -> int:
    value = _required_int(data, key)
    if value < 0:
        raise ProtocolError(f"Protocol field '{key}' must be non-negative.")
    return value


def _required_bool(data: dict[str, object], key: str) -> bool:
    value = data.get(key)
    if type(value) is not bool:
        raise ProtocolError(f"Protocol field '{key}' must be a boolean.")
    return value


def _required_non_negative_number(data: dict[str, object], key: str) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(f"Protocol field '{key}' must be a number.")
    normalized = float(value)
    if normalized < 0:
        raise ProtocolError(f"Protocol field '{key}' must be non-negative.")
    return normalized


def _optional_non_negative_number(
    data: dict[str, object],
    key: str,
    context: str,
) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(f"{context} field '{key}' must be a number or null.")
    normalized = float(value)
    if normalized < 0:
        raise ProtocolError(f"{context} field '{key}' must be non-negative.")
    return normalized


def _optional_positive_int(
    data: dict[str, object],
    key: str,
    context: str,
) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if type(value) is not int:
        raise ProtocolError(f"{context} field '{key}' must be an integer or null.")
    if value < 1:
        raise ProtocolError(f"{context} field '{key}' must be positive.")
    return value


def _required_enum_str(
    data: dict[str, object],
    key: str,
    allowed_values: frozenset[str],
    context: str,
) -> str:
    value = _required_str(data, key)
    if value not in allowed_values:
        allowed = ", ".join(sorted(allowed_values))
        raise ProtocolError(f"{context} field '{key}' must be one of: {allowed}.")
    return value


def _optional_enum_str(
    data: dict[str, object],
    key: str,
    allowed_values: frozenset[str],
    context: str,
) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{context} field '{key}' must be a non-empty string or null.")
    if value not in allowed_values:
        allowed = ", ".join(sorted(allowed_values))
        raise ProtocolError(f"{context} field '{key}' must be one of: {allowed}, or null.")
    return value


def _optional_str(data: dict[str, object], key: str, context: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{context} field '{key}' must be a non-empty string or null.")
    return value


def _required_sha256(data: dict[str, object], key: str) -> str:
    value = _required_str(data, key)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ProtocolError(f"Protocol field '{key}' must be a lowercase SHA-256 hex digest.")
    return value


def _ensure_mapping(data: Mapping[str, object], context: str) -> dict[str, object]:
    if not isinstance(data, dict):
        raise ProtocolError(f"{context} must be a JSON object.")
    return data


def _required_mapping(
    data: dict[str, object],
    key: str,
    context: str,
) -> dict[str, object]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ProtocolError(f"{context} field '{key}' must be a JSON object.")
    return value


def _required_list(
    data: dict[str, object],
    key: str,
    context: str,
) -> list[object]:
    value = data.get(key)
    if not isinstance(value, list):
        raise ProtocolError(f"{context} field '{key}' must be an array.")
    return value


def _required_str_sequence(
    data: dict[str, object],
    key: str,
    context: str,
) -> tuple[str, ...]:
    values = _required_list(data, key, context)
    strings: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value:
            raise ProtocolError(f"{context} field '{key}' item {index} must be a non-empty string.")
        strings.append(value)
    return tuple(strings)


def _require_exact_keys(
    data: Mapping[str, object],
    required_keys: set[str],
    context: str,
) -> None:
    keys = set(data)
    missing = sorted(required_keys - keys)
    if missing:
        raise ProtocolError(f"{context} is missing required field '{missing[0]}'.")
    extra = sorted(keys - required_keys)
    if extra:
        raise ProtocolError(f"{context} contains unsupported field '{extra[0]}'.")


def _validate_capture_metadata_or_summary(data: dict[str, object]) -> None:
    if "segments" in data:
        _validate_capture_metadata(data)
    else:
        _validate_capture_metadata_summary(data)


def _validate_capture_metadata(data: dict[str, object]) -> None:
    _validate_capture_metadata_common(
        data,
        expected_keys={
            "version",
            "profile",
            "sample_rate",
            "channels",
            "segment_seconds",
            "segment_count",
            "segments",
            "sources",
        },
    )
    segment_count = _required_non_negative_int(data, "segment_count")
    segments = _required_list(data, "segments", "capture_metadata")
    if segment_count != len(segments):
        raise ProtocolError("capture_metadata segment_count must match segments length.")
    for segment in segments:
        if not isinstance(segment, dict):
            raise ProtocolError("capture_metadata segments must contain JSON objects.")
        SegmentFlushedPayload.from_dict(segment)


def _validate_capture_metadata_summary(data: dict[str, object]) -> None:
    _validate_capture_metadata_common(
        data,
        expected_keys={
            "version",
            "profile",
            "sample_rate",
            "channels",
            "segment_seconds",
            "segment_count",
            "sources",
        },
    )
    _required_non_negative_int(data, "segment_count")


def _validate_capture_metadata_common(
    data: dict[str, object],
    *,
    expected_keys: set[str],
) -> None:
    _require_exact_keys(data, expected_keys, "capture_metadata")
    if _required_int(data, "version") != 1:
        raise ProtocolError("capture_metadata field 'version' must be 1.")
    _required_enum_str(data, "profile", CAPTURE_PROFILE_IDS, "capture_metadata")
    _required_positive_int(data, "sample_rate")
    _required_positive_int(data, "channels")
    _required_positive_int(data, "segment_seconds")
    sources = _required_list(data, "sources", "capture_metadata")
    for source in sources:
        if not isinstance(source, dict):
            raise ProtocolError("capture_metadata sources must contain JSON objects.")
        _validate_capture_source_summary(source)


def _validate_capture_source_summary(data: dict[str, object]) -> None:
    _require_exact_keys(
        data,
        {
            "source_id",
            "route_id",
            "route_label",
            "capture_mode",
            "channel_layout",
            "segment_count",
        },
        "capture source summary",
    )
    _required_enum_str(data, "source_id", CAPTURE_SOURCE_IDS, "capture source summary")
    _optional_str(data, "route_id", "capture source summary")
    _optional_str(data, "route_label", "capture source summary")
    _optional_enum_str(
        data,
        "capture_mode",
        CAPTURE_MODE_VALUES,
        "capture source summary",
    )
    _required_str(data, "channel_layout")
    _required_non_negative_int(data, "segment_count")
