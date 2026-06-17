"""PipeWire capture adapter and crash-resilient WAV segment writing."""

from __future__ import annotations

import hashlib
import re
import select
import struct
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from mena.profiles import CaptureProfileId, CaptureSourceId

CAPTURE_METADATA_VERSION = 1
DEFAULT_SAMPLE_RATE = 48_000
DEFAULT_SAMPLE_WIDTH_BYTES = 2
DEFAULT_SEGMENT_SECONDS = 30
MAX_SEGMENT_BYTES = 50 * 1024 * 1024
CAPTURE_MODE_SELECTED_MICROPHONE = "selected_microphone"
CAPTURE_MODE_DEDICATED_ROUTE = "dedicated_route"
CAPTURE_MODE_SYSTEM_MIX_REMOTE = "system_mix_remote"
RECOVERY_BACKOFF_SEGMENTS = 4
RECOVERY_PROBE_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True)
class CaptureRoutePlan:
    """Concrete PipeWire route selections for online dual-stream capture."""

    session_id: str
    microphone_route_id: str
    playback_stream_route_id: str | None = None
    output_sink_route_id: str | None = None
    profile_id: CaptureProfileId = CaptureProfileId.ONLINE_DUAL_STREAM
    microphone_route_label: str | None = None
    playback_stream_route_label: str | None = None
    output_sink_route_label: str | None = None
    microphone_backend_name: str | None = None
    output_sink_backend_name: str | None = None
    system_audio_mode: str = CAPTURE_MODE_DEDICATED_ROUTE


@dataclass(frozen=True)
class CaptureSegment:
    """A sealed source audio segment."""

    source_id: CaptureSourceId
    index: int
    path: Path
    sample_rate: int
    channels: int
    channel_layout: str
    sample_format: str
    byte_count: int
    duration_seconds: float
    started_at: str | None
    ended_at: str | None
    sha256: str
    route_id: str | None = None
    route_label: str | None = None
    capture_mode: str | None = None
    status: str = "sealed"
    format: str = "wav"

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id.value,
            "index": self.index,
            "path": str(self.path),
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "channel_layout": self.channel_layout,
            "sample_format": self.sample_format,
            "byte_count": self.byte_count,
            "duration_seconds": self.duration_seconds,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "sha256": self.sha256,
            "route_id": self.route_id,
            "route_label": self.route_label,
            "capture_mode": self.capture_mode,
            "status": self.status,
            "format": self.format,
        }


@dataclass(frozen=True)
class CaptureSourceRoute:
    """Selected OS route facts for one logical capture source."""

    source_id: CaptureSourceId
    route_id: str | None
    route_label: str | None
    capture_mode: str | None = None


@dataclass(frozen=True)
class CaptureRouteEvent:
    """A route-mode degradation or recovery event."""

    event: str
    code: str
    message: str
    source_id: CaptureSourceId | None
    route_id: str | None
    route_label: str | None
    capture_mode: str | None
    elapsed_seconds: float | None = None
    segment_index: int | None = None
    occurred_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "event": self.event,
            "code": self.code,
            "message": self.message,
            "source_id": None if self.source_id is None else self.source_id.value,
            "occurred_at": self.occurred_at,
            "elapsed_seconds": self.elapsed_seconds,
            "segment_index": self.segment_index,
            "route_id": self.route_id,
            "route_label": self.route_label,
            "capture_mode": self.capture_mode,
        }


@dataclass(frozen=True)
class CaptureMetadata:
    """Capture facts emitted by the worker and persisted by the orchestrator."""

    version: int
    profile_id: CaptureProfileId
    sample_rate: int
    channels: int
    segment_seconds: int
    segments: tuple[CaptureSegment, ...]
    source_routes: tuple[CaptureSourceRoute, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "profile": self.profile_id.value,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "segment_seconds": self.segment_seconds,
            "segment_count": len(self.segments),
            "segments": [segment.to_dict() for segment in self.segments],
            "sources": _source_summaries(self.segments, self.source_routes),
        }

    def to_summary_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "profile": self.profile_id.value,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "segment_seconds": self.segment_seconds,
            "segment_count": len(self.segments),
            "sources": _source_summaries(self.segments, self.source_routes),
        }


class WavSegmentWriter:
    """Write sealed WAV source segments without mutating prior segments."""

    def __init__(
        self,
        *,
        segment_dir: Path,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = 1,
        sample_width_bytes: int = DEFAULT_SAMPLE_WIDTH_BYTES,
    ) -> None:
        self.segment_dir = segment_dir
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width_bytes = sample_width_bytes
        self._indexes: dict[CaptureSourceId, int] = {}

    def write_segment(
        self,
        source_id: CaptureSourceId,
        pcm_bytes: bytes,
        *,
        route_id: str | None = None,
        route_label: str | None = None,
        capture_mode: str | None = None,
    ) -> CaptureSegment:
        source_dir = self.segment_dir / source_id.value
        source_dir.mkdir(parents=True, exist_ok=True)
        index = self._next_index(source_id, source_dir)
        path = source_dir / f"{source_id.value}_{index:06d}.wav"
        temp_path = path.with_suffix(".wav.tmp")
        wav_bytes = make_wav_bytes(
            pcm_bytes,
            sample_rate=self.sample_rate,
            channels=self.channels,
            sample_width_bytes=self.sample_width_bytes,
        )
        temp_path.write_bytes(wav_bytes)
        if path.exists():
            temp_path.unlink(missing_ok=True)
            raise FileExistsError(f"Capture segment already exists: {path}")
        temp_path.replace(path)
        self._indexes[source_id] = index
        return CaptureSegment(
            source_id=source_id,
            index=index,
            path=path,
            sample_rate=self.sample_rate,
            channels=self.channels,
            channel_layout=_channel_layout(self.channels),
            sample_format=_sample_format(self.sample_width_bytes),
            byte_count=len(wav_bytes),
            duration_seconds=_duration_seconds(
                len(pcm_bytes),
                sample_rate=self.sample_rate,
                channels=self.channels,
                sample_width_bytes=self.sample_width_bytes,
            ),
            started_at=None,
            ended_at=None,
            sha256=hashlib.sha256(wav_bytes).hexdigest(),
            route_id=route_id,
            route_label=route_label,
            capture_mode=capture_mode,
        )

    def _next_index(self, source_id: CaptureSourceId, source_dir: Path) -> int:
        highest_index = self._indexes.get(source_id, 0)
        prefix = f"{source_id.value}_"
        for path in source_dir.glob(f"{prefix}*.wav"):
            stem = path.stem
            if not stem.startswith(prefix):
                continue
            suffix = stem.removeprefix(prefix)
            if suffix.isdigit():
                highest_index = max(highest_index, int(suffix))
        return highest_index + 1


class SilencePcmProvider:
    """Small deterministic PCM provider used by tests and the current worker slice."""

    def __init__(
        self,
        *,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = 1,
        sample_width_bytes: int = DEFAULT_SAMPLE_WIDTH_BYTES,
        seconds: int = 1,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width_bytes = sample_width_bytes
        self.seconds = seconds

    def read_segment(self, source_id: CaptureSourceId) -> bytes:
        frame_count = self.sample_rate * self.seconds
        return b"\x00" * frame_count * self.channels * self.sample_width_bytes


PcmProvider = Callable[[CaptureSourceId], bytes]


class CaptureSourceError(RuntimeError):
    """A capture source failed while reading aligned source chunks."""

    def __init__(
        self,
        source_id: CaptureSourceId,
        cause: BaseException,
        *,
        partial_pcm_by_source: Mapping[CaptureSourceId, bytes] | None = None,
    ) -> None:
        self.source_id = source_id
        self.cause = cause
        self.partial_pcm_by_source = dict(partial_pcm_by_source or {})
        super().__init__(f"{source_id.value} capture failed: {cause}")


class PipeWirePcmProvider:
    """Continuous raw PCM reader backed by long-lived pw-record processes."""

    def __init__(
        self,
        *,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = 1,
        sample_width_bytes: int = DEFAULT_SAMPLE_WIDTH_BYTES,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width_bytes = sample_width_bytes
        self._popen_factory = popen_factory
        self._streams: dict[tuple[CaptureSourceId, str], _RawPcmCaptureStream] = {}

    def read_segment(
        self,
        source_id: CaptureSourceId,
        *,
        plan: CaptureRoutePlan,
        sink_name: str,
        segment_seconds: int,
        stop_event: threading.Event | None = None,
    ) -> bytes:
        stream = self._stream_for_source(source_id, plan=plan, sink_name=sink_name)
        return stream.read_chunk(
            self._segment_pcm_bytes(segment_seconds),
            frame_width=self.channels * self.sample_width_bytes,
            stop_event=stop_event,
        )

    def read_aligned_segments(
        self,
        *,
        plan: CaptureRoutePlan,
        sink_name: str,
        segment_seconds: int,
        stop_event: threading.Event | None = None,
    ) -> dict[CaptureSourceId, bytes]:
        """Read one aligned chunk from the continuous source streams."""

        sources = (CaptureSourceId.LOCAL_MIC, CaptureSourceId.SYSTEM_AUDIO)
        try:
            streams = {
                source_id: self._stream_for_source(source_id, plan=plan, sink_name=sink_name)
                for source_id in sources
            }
        except Exception:
            self.close()
            raise
        expected_bytes = self._segment_pcm_bytes(segment_seconds)
        frame_width = self.channels * self.sample_width_bytes
        with ThreadPoolExecutor(max_workers=len(streams)) as executor:
            results = {
                source_id: executor.submit(
                    stream.read_chunk,
                    expected_bytes,
                    frame_width=frame_width,
                    stop_event=stop_event,
                )
                for source_id, stream in streams.items()
            }
        output: dict[CaptureSourceId, bytes] = {}
        failure: tuple[CaptureSourceId, BaseException] | None = None
        for source_id, future in results.items():
            try:
                pcm = future.result()
            except BaseException as exc:
                if failure is None:
                    failure = (source_id, exc)
                continue
            if pcm:
                output[source_id] = pcm
        if failure is not None:
            source_id, failure_exc = failure
            raise CaptureSourceError(
                source_id,
                failure_exc,
                partial_pcm_by_source=output,
            ) from failure_exc
        return output

    def probe_source(
        self,
        source_id: CaptureSourceId,
        *,
        plan: CaptureRoutePlan,
        sink_name: str,
        timeout_seconds: float,
    ) -> None:
        """Start and prove a source stream without making it canonical yet."""

        stream = self._stream_for_source(source_id, plan=plan, sink_name=sink_name)
        try:
            stream.read_chunk(
                self.channels * self.sample_width_bytes,
                frame_width=self.channels * self.sample_width_bytes,
                max_wait_seconds=timeout_seconds,
            )
        except Exception:
            self.close_source(source_id, plan=plan, sink_name=sink_name)
            raise

    def close(self) -> None:
        for stream in self._streams.values():
            stream.close()
        self._streams.clear()

    def close_source(
        self,
        source_id: CaptureSourceId,
        *,
        plan: CaptureRoutePlan,
        sink_name: str,
    ) -> None:
        target = self._target_for_source(source_id, plan=plan, sink_name=sink_name)
        stream = self._streams.pop((source_id, target), None)
        if stream is not None:
            stream.close()

    def _stream_for_source(
        self,
        source_id: CaptureSourceId,
        *,
        plan: CaptureRoutePlan,
        sink_name: str,
    ) -> _RawPcmCaptureStream:
        target = self._target_for_source(source_id, plan=plan, sink_name=sink_name)
        key = (source_id, target)
        stream = self._streams.get(key)
        if stream is not None and stream.is_running:
            return stream
        command = self._command_for_source(source_id, plan=plan, sink_name=sink_name)
        process = self._popen_factory(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        stream = _RawPcmCaptureStream(source_id=source_id, command=command, process=process)
        self._streams[key] = stream
        return stream

    def _segment_pcm_bytes(self, segment_seconds: int) -> int:
        return segment_seconds * self.sample_rate * self.channels * self.sample_width_bytes

    def _command_for_source(
        self,
        source_id: CaptureSourceId,
        *,
        plan: CaptureRoutePlan,
        sink_name: str,
    ) -> list[str]:
        target = self._target_for_source(source_id, plan=plan, sink_name=sink_name)
        return [
            "pw-record",
            "--target",
            target,
            "--raw",
            "--format",
            "s16",
            "--rate",
            str(self.sample_rate),
            "--channels",
            str(self.channels),
            "-",
        ]

    def _target_for_source(
        self,
        source_id: CaptureSourceId,
        *,
        plan: CaptureRoutePlan,
        sink_name: str,
    ) -> str:
        if source_id is CaptureSourceId.LOCAL_MIC:
            return _required_backend_target(plan.microphone_backend_name, "microphone")
        if source_id is CaptureSourceId.ROOM_MIC:
            return _required_backend_target(plan.microphone_backend_name, "microphone")
        if source_id is CaptureSourceId.SYSTEM_AUDIO:
            if plan.system_audio_mode == CAPTURE_MODE_SYSTEM_MIX_REMOTE:
                output_sink_target = _required_backend_target(
                    plan.output_sink_backend_name,
                    "output sink",
                )
                return f"{output_sink_target}.monitor"
            if plan.system_audio_mode != CAPTURE_MODE_DEDICATED_ROUTE:
                raise ValueError(
                    f"Unsupported system audio capture mode '{plan.system_audio_mode}'."
                )
            return f"{sink_name}.monitor"
        raise ValueError(f"Unsupported capture source '{source_id}'.")


class _RawPcmCaptureStream:
    """One long-lived raw PCM capture stream for a logical source."""

    def __init__(
        self,
        *,
        source_id: CaptureSourceId,
        command: list[str],
        process: subprocess.Popen[bytes],
    ) -> None:
        self.source_id = source_id
        self.command = command
        self.process = process

    @property
    def is_running(self) -> bool:
        poll = getattr(self.process, "poll", None)
        if not callable(poll):
            return True
        return poll() is None

    def read_chunk(
        self,
        expected_bytes: int,
        *,
        frame_width: int,
        stop_event: threading.Event | None = None,
        max_wait_seconds: float | None = None,
    ) -> bytes:
        if expected_bytes < 1:
            raise ValueError("PCM chunk size must be positive.")
        if frame_width < 1:
            raise ValueError("PCM frame width must be positive.")
        stdout = self.process.stdout
        if stdout is None:
            raise subprocess.CalledProcessError(
                1,
                self.command,
                stderr=b"pw-record did not expose stdout",
            )
        chunks: list[bytes] = []
        remaining = expected_bytes
        deadline = None if max_wait_seconds is None else time.monotonic() + max_wait_seconds
        while remaining > 0:
            if stop_event is not None and stop_event.is_set():
                self.close()
                break
            timeout = 0.25
            if deadline is not None:
                timeout = min(timeout, max(0.0, deadline - time.monotonic()))
                if timeout <= 0:
                    break
            if not _pipe_has_data(stdout, timeout=timeout):
                if not self.is_running:
                    break
                continue
            chunk = stdout.read(min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        pcm = b"".join(chunks)
        aligned_length = len(pcm) - (len(pcm) % frame_width)
        pcm = pcm[:aligned_length]
        if pcm:
            if remaining > 0 and not (stop_event is not None and stop_event.is_set()):
                if deadline is not None and time.monotonic() >= deadline:
                    assert max_wait_seconds is not None
                    raise subprocess.TimeoutExpired(self.command, max_wait_seconds, output=pcm)
                returncode = getattr(self.process, "returncode", None)
                if returncode is None:
                    poll = getattr(self.process, "poll", None)
                    if callable(poll):
                        returncode = poll()
                stderr = _read_process_stderr(self.process)
                raise subprocess.CalledProcessError(
                    1 if returncode is None else returncode,
                    self.command,
                    output=pcm,
                    stderr=stderr,
                )
            return pcm
        if stop_event is not None and stop_event.is_set():
            return b""
        if deadline is not None and time.monotonic() >= deadline:
            assert max_wait_seconds is not None
            raise subprocess.TimeoutExpired(self.command, max_wait_seconds)
        returncode = getattr(self.process, "returncode", None)
        if returncode is None:
            poll = getattr(self.process, "poll", None)
            if callable(poll):
                returncode = poll()
        stderr = _read_process_stderr(self.process)
        raise subprocess.CalledProcessError(
            1 if returncode is None else returncode,
            self.command,
            output=pcm,
            stderr=stderr,
        )

    def close(self) -> None:
        if not self.is_running:
            return
        terminate = getattr(self.process, "terminate", None)
        kill = getattr(self.process, "kill", None)
        communicate = getattr(self.process, "communicate", None)
        if callable(terminate):
            terminate()
        elif callable(kill):
            kill()
        if callable(communicate):
            try:
                communicate(timeout=1)
            except subprocess.TimeoutExpired:
                if callable(kill):
                    kill()
                communicate()


class PipeWireClient:
    """Narrow command wrapper for PipeWire/Pulse-compatible routing commands."""

    def __init__(
        self, *, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    ) -> None:
        self._runner = runner

    def create_null_sink(self, sink_name: str) -> str:
        return self._run(
            [
                "pactl",
                "load-module",
                "module-null-sink",
                f"sink_name={sink_name}",
                f"sink_properties=device.description={sink_name}",
            ]
        )

    def move_playback_stream(self, playback_stream_route_id: str, sink_name: str) -> None:
        self._run(
            ["pactl", "move-sink-input", _pulse_sink_input_id(playback_stream_route_id), sink_name]
        )

    def create_loopback(self, sink_name: str, output_sink_name: str) -> str:
        return self._run(
            [
                "pactl",
                "load-module",
                "module-loopback",
                f"source={sink_name}.monitor",
                f"sink={output_sink_name}",
            ]
        )

    def unload_module(self, module_id: str) -> None:
        self._run(["pactl", "unload-module", module_id])

    def _run(self, args: list[str]) -> str:
        completed = self._runner(args, capture_output=True, text=True, check=True)
        return completed.stdout.strip()


class PipeWireCaptureAdapter:
    """Apply online dual-stream routing and emit sealed local/remote WAV segments."""

    def __init__(
        self,
        *,
        client: PipeWireClient,
        segment_writer: WavSegmentWriter,
        pcm_provider: PcmProvider | None = None,
        segment_seconds: int = DEFAULT_SEGMENT_SECONDS,
    ) -> None:
        self.client = client
        self.segment_writer = segment_writer
        self.pcm_provider = pcm_provider
        self.pipewire_pcm_provider = PipeWirePcmProvider(
            sample_rate=segment_writer.sample_rate,
            channels=segment_writer.channels,
        )
        self.segment_seconds = segment_seconds

    def start(self, plan: CaptureRoutePlan) -> ActivePipeWireCapture:
        sink_name = session_null_sink_name(plan.session_id)
        if plan.profile_id is CaptureProfileId.IN_PERSON_SINGLE_MIC:
            return ActivePipeWireCapture(
                adapter=self,
                plan=plan,
                sink_name=sink_name,
                null_sink_module=None,
                loopback_module=None,
            )
        if plan.playback_stream_route_id is None:
            raise ValueError("Online capture requires a playback stream route.")
        if plan.output_sink_route_id is None:
            raise ValueError("Online capture requires an output sink route.")
        try:
            null_sink_module, loopback_module = self._setup_dedicated_route(
                plan=plan,
                sink_name=sink_name,
            )
            route_events: tuple[CaptureRouteEvent, ...] = ()
        except Exception:
            plan = replace(plan, system_audio_mode=CAPTURE_MODE_SYSTEM_MIX_REMOTE)
            null_sink_module = None
            loopback_module = None
            route_events = (
                _route_event(
                    event="degraded",
                    code="dedicated_route_unavailable",
                    message=(
                        "Dedicated meeting route unavailable; recording selected output "
                        "sink monitor as system-mix remote audio."
                    ),
                    source_id=CaptureSourceId.SYSTEM_AUDIO,
                    plan=plan,
                    segment_index=None,
                ),
            )
        return ActivePipeWireCapture(
            adapter=self,
            plan=plan,
            sink_name=sink_name,
            null_sink_module=null_sink_module,
            loopback_module=loopback_module,
            route_events=route_events,
        )

    def _flush_once(
        self,
        *,
        plan: CaptureRoutePlan,
        sink_name: str,
        stop_event: threading.Event | None = None,
        prefilled_pcm_by_source: Mapping[CaptureSourceId, bytes] | None = None,
    ) -> tuple[CaptureSegment, ...]:
        if plan.profile_id is CaptureProfileId.IN_PERSON_SINGLE_MIC:
            room_pcm = self._read_pcm(
                CaptureSourceId.ROOM_MIC,
                plan=plan,
                sink_name=sink_name,
                stop_event=stop_event,
            )
            if not room_pcm:
                return ()
            return (self._write_segment(CaptureSourceId.ROOM_MIC, room_pcm, plan=plan),)
        if self.pcm_provider is None:
            pcm_by_source = dict(prefilled_pcm_by_source or {})
            missing_sources = tuple(
                source_id
                for source_id in (CaptureSourceId.LOCAL_MIC, CaptureSourceId.SYSTEM_AUDIO)
                if source_id not in pcm_by_source
            )
            if len(missing_sources) == 2:
                pcm_by_source = self.pipewire_pcm_provider.read_aligned_segments(
                    plan=plan,
                    sink_name=sink_name,
                    segment_seconds=self.segment_seconds,
                    stop_event=stop_event,
                )
            else:
                for source_id in missing_sources:
                    pcm = self._read_pcm(
                        source_id,
                        plan=plan,
                        sink_name=sink_name,
                        stop_event=stop_event,
                    )
                    if pcm:
                        pcm_by_source[source_id] = pcm
            segments: list[CaptureSegment] = []
            for source_id in (CaptureSourceId.LOCAL_MIC, CaptureSourceId.SYSTEM_AUDIO):
                source_pcm = pcm_by_source.get(source_id)
                if source_pcm:
                    segments.append(self._write_segment(source_id, source_pcm, plan=plan))
            return tuple(segments)
        segments = []
        for source_id in (CaptureSourceId.LOCAL_MIC, CaptureSourceId.SYSTEM_AUDIO):
            pcm = self._read_pcm(
                source_id,
                plan=plan,
                sink_name=sink_name,
                stop_event=stop_event,
            )
            if pcm:
                segments.append(self._write_segment(source_id, pcm, plan=plan))
        return tuple(segments)

    def _write_segment(
        self,
        source_id: CaptureSourceId,
        pcm: bytes,
        *,
        plan: CaptureRoutePlan,
    ) -> CaptureSegment:
        route_id, route_label, capture_mode = _segment_route_facts(plan, source_id)
        return self.segment_writer.write_segment(
            source_id,
            pcm,
            route_id=route_id,
            route_label=route_label,
            capture_mode=capture_mode,
        )

    def _read_pcm(
        self,
        source_id: CaptureSourceId,
        *,
        plan: CaptureRoutePlan,
        sink_name: str,
        stop_event: threading.Event | None = None,
    ) -> bytes:
        if self.pcm_provider:
            return self.pcm_provider(source_id)
        return self.pipewire_pcm_provider.read_segment(
            source_id,
            plan=plan,
            sink_name=sink_name,
            segment_seconds=self.segment_seconds,
            stop_event=stop_event,
        )

    def _cleanup(self, *, null_sink_module: str | None, loopback_module: str | None) -> None:
        self.pipewire_pcm_provider.close()
        self._cleanup_routes(null_sink_module=null_sink_module, loopback_module=loopback_module)

    def _cleanup_routes(
        self,
        *,
        null_sink_module: str | None,
        loopback_module: str | None,
    ) -> None:
        for module_id in (loopback_module, null_sink_module):
            if module_id:
                try:
                    self.client.unload_module(module_id)
                except (subprocess.CalledProcessError, OSError):
                    continue

    def _setup_dedicated_route(
        self,
        *,
        plan: CaptureRoutePlan,
        sink_name: str,
    ) -> tuple[str, str | None]:
        null_sink_module = self.client.create_null_sink(sink_name)
        loopback_module: str | None = None
        playback_moved = False
        try:
            if plan.playback_stream_route_id is None:
                raise ValueError("Online capture requires a playback stream route.")
            if plan.output_sink_route_id is None:
                raise ValueError("Online capture requires an output sink route.")
            if plan.output_sink_backend_name is None:
                raise ValueError("Online capture requires an output sink backend name.")
            self.client.move_playback_stream(plan.playback_stream_route_id, sink_name)
            playback_moved = True
            loopback_module = self.client.create_loopback(
                sink_name,
                plan.output_sink_backend_name,
            )
        except Exception:
            if playback_moved:
                self._restore_playback_route(plan)
            self._cleanup_routes(
                null_sink_module=null_sink_module,
                loopback_module=loopback_module,
            )
            raise
        return null_sink_module, loopback_module

    def _restore_playback_route(self, plan: CaptureRoutePlan) -> None:
        if plan.playback_stream_route_id is None or plan.output_sink_backend_name is None:
            return
        try:
            self.client.move_playback_stream(
                plan.playback_stream_route_id,
                plan.output_sink_backend_name,
            )
        except (subprocess.CalledProcessError, OSError, ValueError):
            return


class ActivePipeWireCapture:
    """Active capture routing state owned by the worker."""

    def __init__(
        self,
        *,
        adapter: PipeWireCaptureAdapter,
        plan: CaptureRoutePlan,
        sink_name: str,
        null_sink_module: str | None,
        loopback_module: str | None,
        route_events: tuple[CaptureRouteEvent, ...] = (),
    ) -> None:
        self._adapter = adapter
        self._plan = plan
        self.sink_name = sink_name
        self.null_sink_module = null_sink_module
        self.loopback_module = loopback_module
        self._segments: list[CaptureSegment] = []
        self._route_events: list[CaptureRouteEvent] = list(route_events)
        self._stopped = False
        self._next_recovery_after_system_audio_segment_count = 1
        self._pending_recovered_system_mix_plan: CaptureRoutePlan | None = None
        self._pending_recovery_event: CaptureRouteEvent | None = None

    def flush_segments(
        self, *, stop_event: threading.Event | None = None
    ) -> tuple[CaptureSegment, ...]:
        if self._stopped:
            return ()
        if self._should_attempt_recovery():
            self._try_recover_dedicated_route()
        try:
            segments = self._adapter._flush_once(
                plan=self._plan,
                sink_name=self.sink_name,
                stop_event=stop_event,
            )
        except CaptureSourceError as exc:
            if self._can_degrade_to_system_mix(exc):
                self._degrade_to_system_mix()
                segments = self._adapter._flush_once(
                    plan=self._plan,
                    sink_name=self.sink_name,
                    stop_event=stop_event,
                    prefilled_pcm_by_source=exc.partial_pcm_by_source,
                )
            else:
                raise
        self._segments.extend(segments)
        self._finish_pending_recovery_cleanup(segments)
        return segments

    def drain_route_events(self) -> tuple[CaptureRouteEvent, ...]:
        events = tuple(self._route_events)
        self._route_events.clear()
        return events

    def stop(self) -> CaptureMetadata:
        if not self._stopped:
            self._adapter._cleanup(
                null_sink_module=self.null_sink_module,
                loopback_module=self.loopback_module,
            )
            self._stopped = True
        return CaptureMetadata(
            version=CAPTURE_METADATA_VERSION,
            profile_id=self._plan.profile_id,
            sample_rate=self._adapter.segment_writer.sample_rate,
            channels=self._adapter.segment_writer.channels,
            segment_seconds=self._adapter.segment_seconds,
            segments=tuple(self._segments),
        )

    def _can_degrade_to_system_mix(self, exc: CaptureSourceError) -> bool:
        return (
            self._plan.profile_id is CaptureProfileId.ONLINE_DUAL_STREAM
            and self._plan.system_audio_mode == CAPTURE_MODE_DEDICATED_ROUTE
            and exc.source_id is CaptureSourceId.SYSTEM_AUDIO
            and self._plan.output_sink_route_id is not None
        )

    def _degrade_to_system_mix(self) -> None:
        dedicated_plan = self._plan
        pending_recovery_failed = self._pending_recovered_system_mix_plan is not None
        self._adapter._cleanup_routes(
            null_sink_module=self.null_sink_module,
            loopback_module=self.loopback_module,
        )
        close_source = getattr(self._adapter.pipewire_pcm_provider, "close_source", None)
        if callable(close_source):
            close_source(
                CaptureSourceId.SYSTEM_AUDIO,
                plan=dedicated_plan,
                sink_name=self.sink_name,
            )
        self.null_sink_module = None
        self.loopback_module = None
        self._plan = self._pending_recovered_system_mix_plan or replace(
            self._plan,
            system_audio_mode=CAPTURE_MODE_SYSTEM_MIX_REMOTE,
        )
        self._pending_recovered_system_mix_plan = None
        self._pending_recovery_event = None
        if pending_recovery_failed:
            self._defer_next_recovery_attempt()
        else:
            self._next_recovery_after_system_audio_segment_count = 1
        self._route_events.append(
            _route_event(
                event="degraded",
                code="dedicated_route_runtime_failed",
                message=(
                    "Dedicated meeting route failed during capture; recording selected "
                    "output sink monitor as system-mix remote audio."
                ),
                source_id=CaptureSourceId.SYSTEM_AUDIO,
                plan=self._plan,
                segment_index=self._next_segment_index(CaptureSourceId.SYSTEM_AUDIO),
            )
        )

    def _should_attempt_recovery(self) -> bool:
        fallback_segment_count = self._source_segment_count(CaptureSourceId.SYSTEM_AUDIO)
        return (
            self._plan.profile_id is CaptureProfileId.ONLINE_DUAL_STREAM
            and self._plan.system_audio_mode == CAPTURE_MODE_SYSTEM_MIX_REMOTE
            and fallback_segment_count > 0
            and fallback_segment_count >= self._next_recovery_after_system_audio_segment_count
        )

    def _try_recover_dedicated_route(self) -> None:
        fallback_plan = self._plan
        primary_plan = replace(self._plan, system_audio_mode=CAPTURE_MODE_DEDICATED_ROUTE)
        try:
            null_sink_module, loopback_module = self._adapter._setup_dedicated_route(
                plan=primary_plan,
                sink_name=self.sink_name,
            )
        except Exception:
            self._defer_next_recovery_attempt()
            return
        try:
            self._adapter.pipewire_pcm_provider.probe_source(
                CaptureSourceId.SYSTEM_AUDIO,
                plan=primary_plan,
                sink_name=self.sink_name,
                timeout_seconds=RECOVERY_PROBE_TIMEOUT_SECONDS,
            )
        except Exception:
            self._adapter._cleanup_routes(
                null_sink_module=null_sink_module,
                loopback_module=loopback_module,
            )
            self._defer_next_recovery_attempt()
            return
        self.null_sink_module = null_sink_module
        self.loopback_module = loopback_module
        self._pending_recovered_system_mix_plan = fallback_plan
        self._plan = primary_plan
        self._pending_recovery_event = _route_event(
            event="recovered",
            code="dedicated_route_recovered",
            message="Dedicated meeting route recovered; recording isolated meeting playback.",
            source_id=CaptureSourceId.SYSTEM_AUDIO,
            plan=self._plan,
            segment_index=self._next_segment_index(CaptureSourceId.SYSTEM_AUDIO),
        )

    def _defer_next_recovery_attempt(self) -> None:
        self._next_recovery_after_system_audio_segment_count = (
            self._source_segment_count(CaptureSourceId.SYSTEM_AUDIO) + RECOVERY_BACKOFF_SEGMENTS
        )

    def _finish_pending_recovery_cleanup(self, segments: tuple[CaptureSegment, ...]) -> None:
        fallback_plan = self._pending_recovered_system_mix_plan
        if fallback_plan is None:
            return
        recovered_system_audio = any(
            segment.source_id is CaptureSourceId.SYSTEM_AUDIO
            and segment.capture_mode == CAPTURE_MODE_DEDICATED_ROUTE
            for segment in segments
        )
        if not recovered_system_audio:
            return
        close_source = getattr(self._adapter.pipewire_pcm_provider, "close_source", None)
        if callable(close_source):
            close_source(
                CaptureSourceId.SYSTEM_AUDIO,
                plan=fallback_plan,
                sink_name=self.sink_name,
            )
        if self._pending_recovery_event is not None:
            self._route_events.append(self._pending_recovery_event)
            self._pending_recovery_event = None
        self._pending_recovered_system_mix_plan = None

    def _source_segment_count(self, source_id: CaptureSourceId) -> int:
        return sum(1 for segment in self._segments if segment.source_id is source_id)

    def _next_segment_index(self, source_id: CaptureSourceId) -> int:
        return self._source_segment_count(source_id) + 1


def session_null_sink_name(session_id: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_]", "_", session_id)[:48].strip("_")
    if not sanitized:
        sanitized = "session"
    return f"mena_{sanitized}_remote"


def make_wav_bytes(
    pcm_bytes: bytes,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    channels: int = 1,
    sample_width_bytes: int = DEFAULT_SAMPLE_WIDTH_BYTES,
) -> bytes:
    data_size = len(pcm_bytes)
    if data_size > MAX_SEGMENT_BYTES:
        raise ValueError(f"Segment exceeds {MAX_SEGMENT_BYTES} byte limit.")
    if sample_rate < 1:
        raise ValueError("WAV sample_rate must be positive.")
    if channels < 1:
        raise ValueError("WAV channels must be positive.")
    if sample_width_bytes < 1:
        raise ValueError("WAV sample_width_bytes must be positive.")
    block_align = channels * sample_width_bytes
    if data_size % block_align != 0:
        raise ValueError("PCM byte length must align to complete WAV frames.")
    byte_rate = sample_rate * channels * sample_width_bytes
    bits_per_sample = sample_width_bytes * 8
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )
    return header + pcm_bytes


def _required_backend_target(backend_name: str | None, role: str) -> str:
    if backend_name is None:
        raise ValueError(f"PipeWire {role} target requires a backend name.")
    return backend_name


def _segment_route_facts(
    plan: CaptureRoutePlan,
    source_id: CaptureSourceId,
) -> tuple[str | None, str | None, str | None]:
    if source_id in {CaptureSourceId.LOCAL_MIC, CaptureSourceId.ROOM_MIC}:
        return (
            plan.microphone_route_id,
            plan.microphone_route_label,
            CAPTURE_MODE_SELECTED_MICROPHONE,
        )
    if source_id is CaptureSourceId.SYSTEM_AUDIO:
        if plan.system_audio_mode == CAPTURE_MODE_SYSTEM_MIX_REMOTE:
            return (
                plan.output_sink_route_id,
                plan.output_sink_route_label,
                CAPTURE_MODE_SYSTEM_MIX_REMOTE,
            )
        return (
            plan.playback_stream_route_id,
            plan.playback_stream_route_label,
            CAPTURE_MODE_DEDICATED_ROUTE,
        )
    return (None, None, None)


def _route_event(
    *,
    event: str,
    code: str,
    message: str,
    source_id: CaptureSourceId,
    plan: CaptureRoutePlan,
    segment_index: int | None,
) -> CaptureRouteEvent:
    route_id, route_label, capture_mode = _segment_route_facts(plan, source_id)
    return CaptureRouteEvent(
        event=event,
        code=code,
        message=message,
        source_id=source_id,
        route_id=route_id,
        route_label=route_label,
        capture_mode=capture_mode,
        elapsed_seconds=None,
        segment_index=segment_index,
    )


def _pipe_has_data(pipe: object, *, timeout: float) -> bool:
    fileno = getattr(pipe, "fileno", None)
    if not callable(fileno):
        return True
    try:
        fd = fileno()
    except (OSError, ValueError):
        return True
    readable, _, _ = select.select([fd], [], [], timeout)
    return bool(readable)


def _read_process_stderr(process: subprocess.Popen[bytes]) -> bytes:
    stderr = process.stderr
    if stderr is None:
        return b""
    if not _pipe_has_data(stderr, timeout=0):
        return b""
    data = stderr.read()
    if isinstance(data, bytes):
        return data
    return b""


def _channel_layout(channels: int) -> str:
    if channels == 1:
        return "mono"
    if channels == 2:
        return "stereo"
    return f"{channels}_channels"


def _sample_format(sample_width_bytes: int) -> str:
    return f"pcm_s{sample_width_bytes * 8}le"


def _duration_seconds(
    pcm_byte_count: int,
    *,
    sample_rate: int,
    channels: int,
    sample_width_bytes: int,
) -> float:
    frame_width = channels * sample_width_bytes
    if frame_width <= 0 or sample_rate <= 0:
        return 0.0
    return pcm_byte_count / frame_width / sample_rate


def _source_summaries(
    segments: tuple[CaptureSegment, ...],
    source_routes: tuple[CaptureSourceRoute, ...] = (),
) -> list[dict[str, object]]:
    routes_by_source = {route.source_id: route for route in source_routes}
    ordered_source_ids = tuple(dict.fromkeys(segment.source_id for segment in segments))
    summaries: list[dict[str, object]] = []
    for source_id in ordered_source_ids:
        source_segments = [segment for segment in segments if segment.source_id is source_id]
        channel_layout = source_segments[-1].channel_layout if source_segments else "unknown"
        source_route = routes_by_source.get(source_id)
        summaries.append(
            {
                "source_id": source_id.value,
                "route_id": source_segments[-1].route_id
                or (None if source_route is None else source_route.route_id),
                "route_label": source_segments[-1].route_label
                or (None if source_route is None else source_route.route_label),
                "capture_mode": source_segments[-1].capture_mode
                or (None if source_route is None else source_route.capture_mode),
                "channel_layout": channel_layout,
                "segment_count": len(source_segments),
            }
        )
    return summaries


def _pulse_sink_input_id(route_id: str) -> str:
    prefix = "pulse:sink-input:"
    if not route_id.startswith(prefix):
        raise ValueError(f"Unsupported Pulse sink-input route id '{route_id}'.")
    sink_input_id = route_id.removeprefix(prefix)
    if not sink_input_id.isdigit():
        raise ValueError(f"Unsupported Pulse sink-input route id '{route_id}'.")
    return sink_input_id
