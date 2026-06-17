"""PipeWire capture adapter and crash-resilient WAV segment writing."""

from __future__ import annotations

import hashlib
import re
import select
import struct
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from mena.profiles import CaptureProfileId, CaptureSourceId

CAPTURE_METADATA_VERSION = 1
DEFAULT_SAMPLE_RATE = 48_000
DEFAULT_SAMPLE_WIDTH_BYTES = 2
DEFAULT_SEGMENT_SECONDS = 30
MAX_SEGMENT_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class CaptureRoutePlan:
    """Concrete PipeWire route selections for online dual-stream capture."""

    session_id: str
    microphone_route_id: str
    playback_stream_route_id: str | None = None
    output_sink_route_id: str | None = None
    profile_id: CaptureProfileId = CaptureProfileId.ONLINE_DUAL_STREAM


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
            "status": self.status,
            "format": self.format,
        }


@dataclass(frozen=True)
class CaptureSourceRoute:
    """Selected OS route facts for one logical capture source."""

    source_id: CaptureSourceId
    route_id: str | None
    route_label: str | None


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

    def write_segment(self, source_id: CaptureSourceId, pcm_bytes: bytes) -> CaptureSegment:
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
        for source_id, future in results.items():
            pcm = future.result()
            if pcm:
                output[source_id] = pcm
        return output

    def close(self) -> None:
        for stream in self._streams.values():
            stream.close()
        self._streams.clear()

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
            return _pipewire_node_id(plan.microphone_route_id)
        if source_id is CaptureSourceId.ROOM_MIC:
            return _pipewire_node_id(plan.microphone_route_id)
        if source_id is CaptureSourceId.SYSTEM_AUDIO:
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
        while remaining > 0:
            if stop_event is not None and stop_event.is_set():
                self.close()
                break
            if not _pipe_has_data(stdout, timeout=0.25):
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
            return pcm
        if stop_event is not None and stop_event.is_set():
            return b""
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

    def create_loopback(self, sink_name: str, output_sink_route_id: str) -> str:
        return self._run(
            [
                "pactl",
                "load-module",
                "module-loopback",
                f"source={sink_name}.monitor",
                f"sink={_pipewire_node_id(output_sink_route_id)}",
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
        null_sink_module = self.client.create_null_sink(sink_name)
        loopback_module: str | None = None
        try:
            self.client.move_playback_stream(plan.playback_stream_route_id, sink_name)
            loopback_module = self.client.create_loopback(sink_name, plan.output_sink_route_id)
        except Exception:
            self._cleanup(null_sink_module=null_sink_module, loopback_module=loopback_module)
            raise
        return ActivePipeWireCapture(
            adapter=self,
            plan=plan,
            sink_name=sink_name,
            null_sink_module=null_sink_module,
            loopback_module=loopback_module,
        )

    def _flush_once(
        self,
        *,
        plan: CaptureRoutePlan,
        sink_name: str,
        stop_event: threading.Event | None = None,
    ) -> tuple[CaptureSegment, ...]:
        if plan.profile_id is CaptureProfileId.IN_PERSON_SINGLE_MIC:
            room = self.segment_writer.write_segment(
                CaptureSourceId.ROOM_MIC,
                self._read_pcm(
                    CaptureSourceId.ROOM_MIC,
                    plan=plan,
                    sink_name=sink_name,
                    stop_event=stop_event,
                ),
            )
            return (room,)
        if self.pcm_provider is None:
            pcm_by_source = self.pipewire_pcm_provider.read_aligned_segments(
                plan=plan,
                sink_name=sink_name,
                segment_seconds=self.segment_seconds,
                stop_event=stop_event,
            )
            segments: list[CaptureSegment] = []
            for source_id in (CaptureSourceId.LOCAL_MIC, CaptureSourceId.SYSTEM_AUDIO):
                pcm = pcm_by_source.get(source_id)
                if pcm:
                    segments.append(self.segment_writer.write_segment(source_id, pcm))
            return tuple(segments)
        local = self.segment_writer.write_segment(
            CaptureSourceId.LOCAL_MIC,
            self._read_pcm(
                CaptureSourceId.LOCAL_MIC,
                plan=plan,
                sink_name=sink_name,
                stop_event=stop_event,
            ),
        )
        remote = self.segment_writer.write_segment(
            CaptureSourceId.SYSTEM_AUDIO,
            self._read_pcm(
                CaptureSourceId.SYSTEM_AUDIO,
                plan=plan,
                sink_name=sink_name,
                stop_event=stop_event,
            ),
        )
        return (local, remote)

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
        for module_id in (loopback_module, null_sink_module):
            if module_id:
                try:
                    self.client.unload_module(module_id)
                except (subprocess.CalledProcessError, OSError):
                    continue


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
    ) -> None:
        self._adapter = adapter
        self._plan = plan
        self.sink_name = sink_name
        self.null_sink_module = null_sink_module
        self.loopback_module = loopback_module
        self._segments: list[CaptureSegment] = []
        self._stopped = False

    def flush_segments(
        self, *, stop_event: threading.Event | None = None
    ) -> tuple[CaptureSegment, ...]:
        if self._stopped:
            return ()
        segments = self._adapter._flush_once(
            plan=self._plan,
            sink_name=self.sink_name,
            stop_event=stop_event,
        )
        self._segments.extend(segments)
        return segments

    def stop(self) -> CaptureMetadata:
        if not self._stopped:
            try:
                if not self._segments:
                    self.flush_segments()
            finally:
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


def _pipewire_node_id(route_id: str) -> str:
    prefix = "pipewire:node:"
    if not route_id.startswith(prefix):
        raise ValueError(f"Unsupported PipeWire route id '{route_id}'.")
    node_id = route_id.removeprefix(prefix)
    if not node_id.isdigit():
        raise ValueError(f"Unsupported PipeWire route id '{route_id}'.")
    return node_id


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
                "route_id": None if source_route is None else source_route.route_id,
                "route_label": None if source_route is None else source_route.route_label,
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
