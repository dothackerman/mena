"""Internal stdio JSON-L protocol for orchestrator and capture worker."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import IO, Self
from uuid import uuid4

PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0
ENVELOPE_VERSION = 1
MAX_FRAME_BYTES = 64 * 1024

MESSAGE_TYPES = frozenset(
    {
        "hello",
        "hello_ack",
        "start_capture",
        "stop_capture",
        "capture_started",
        "segment_flushed",
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
        validate_message_type(self.type)
        if not isinstance(self.id, str) or not self.id:
            raise ProtocolError("Protocol id must be a non-empty string.")
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ProtocolError("Protocol session_id must be a non-empty string.")
        if not isinstance(self.payload, dict):
            raise ProtocolError("Protocol payload must be a JSON object.")
        frame = {
            "v": self.v,
            "type": self.type,
            "id": self.id,
            "session_id": self.session_id,
            "payload": self.payload,
        }
        encoded = (
            json.dumps(frame, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n"
        )
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
        command = argv or [
            sys.executable,
            "-m",
            "mena.capture_worker",
            "--session-id",
            session_id,
            "--session-dir",
            str(session_dir),
        ]
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )

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

    def start_capture(self, payload: dict[str, object] | None = None) -> ProtocolMessage:
        self.send(
            ProtocolMessage(type="start_capture", session_id=self.session_id, payload=payload or {})
        )
        return self.receive_expected("capture_started")

    def stop_capture(self) -> ProtocolMessage:
        self.send(ProtocolMessage(type="stop_capture", session_id=self.session_id))
        while True:
            message = self.receive()
            if message.type == "capture_finalized":
                return message
            if message.type == "error":
                raise ProtocolError(str(message.payload.get("message", "Worker capture error.")))

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
        if self.process.stderr is None:
            return ""
        return self.process.stderr.read().decode("utf-8", errors="replace")


def _required_str(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"Protocol field '{key}' must be a non-empty string.")
    return value


def _required_int(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        raise ProtocolError(f"Protocol field '{key}' must be an integer.")
    return value
