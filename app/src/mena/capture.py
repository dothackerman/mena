"""PipeWire capture adapter and crash-resilient WAV segment writing."""

from __future__ import annotations

import hashlib
import re
import struct
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from mena.profiles import CaptureSourceId

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
    playback_stream_route_id: str
    output_sink_route_id: str


@dataclass(frozen=True)
class CaptureSegment:
    """A sealed source audio segment."""

    source_id: CaptureSourceId
    index: int
    path: Path
    sample_rate: int
    channels: int
    byte_count: int
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
            "byte_count": self.byte_count,
            "sha256": self.sha256,
            "status": self.status,
            "format": self.format,
        }


@dataclass(frozen=True)
class CaptureMetadata:
    """Capture facts emitted by the worker and persisted by the orchestrator."""

    version: int
    sample_rate: int
    channels: int
    segment_seconds: int
    segments: tuple[CaptureSegment, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "segment_seconds": self.segment_seconds,
            "segment_count": len(self.segments),
            "segments": [segment.to_dict() for segment in self.segments],
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
            byte_count=len(wav_bytes),
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
    """Bounded raw PCM reader backed by pw-record."""

    def __init__(
        self,
        *,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = 1,
        sample_width_bytes: int = DEFAULT_SAMPLE_WIDTH_BYTES,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width_bytes = sample_width_bytes
        self._runner = runner
        self._popen_factory = popen_factory

    def read_segment(
        self,
        source_id: CaptureSourceId,
        *,
        plan: CaptureRoutePlan,
        sink_name: str,
        segment_seconds: int,
    ) -> bytes:
        command = self._command_for_source(source_id, plan=plan, sink_name=sink_name)
        try:
            completed = self._runner(
                command, capture_output=True, check=True, timeout=segment_seconds
            )
        except subprocess.TimeoutExpired as exc:
            output = exc.output or b""
            return output if isinstance(output, bytes) else output.encode("utf-8")
        return completed.stdout

    def read_aligned_segments(
        self,
        *,
        plan: CaptureRoutePlan,
        sink_name: str,
        segment_seconds: int,
    ) -> dict[CaptureSourceId, bytes]:
        """Record both sources inside the same bounded segment window."""

        sources = (CaptureSourceId.LOCAL_MIC, CaptureSourceId.SYSTEM_AUDIO)
        commands = {
            source_id: self._command_for_source(source_id, plan=plan, sink_name=sink_name)
            for source_id in sources
        }
        processes = {
            source_id: self._popen_factory(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
            for source_id, command in commands.items()
        }
        with ThreadPoolExecutor(max_workers=len(processes)) as executor:
            results = {
                source_id: executor.submit(_communicate_bounded, process, segment_seconds)
                for source_id, process in processes.items()
            }
        output: dict[CaptureSourceId, bytes] = {}
        for source_id, future in results.items():
            stdout, stderr, returncode = future.result()
            if returncode:
                raise subprocess.CalledProcessError(
                    returncode,
                    commands[source_id],
                    output=stdout,
                    stderr=stderr,
                )
            output[source_id] = stdout
        return output

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
        if source_id is CaptureSourceId.SYSTEM_AUDIO:
            return f"{sink_name}.monitor"
        raise ValueError(f"Unsupported capture source '{source_id}'.")


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
    ) -> tuple[CaptureSegment, CaptureSegment]:
        if self.pcm_provider is None:
            pcm_by_source = self.pipewire_pcm_provider.read_aligned_segments(
                plan=plan,
                sink_name=sink_name,
                segment_seconds=self.segment_seconds,
            )
            local = self.segment_writer.write_segment(
                CaptureSourceId.LOCAL_MIC,
                pcm_by_source[CaptureSourceId.LOCAL_MIC],
            )
            remote = self.segment_writer.write_segment(
                CaptureSourceId.SYSTEM_AUDIO,
                pcm_by_source[CaptureSourceId.SYSTEM_AUDIO],
            )
            return (local, remote)
        local = self.segment_writer.write_segment(
            CaptureSourceId.LOCAL_MIC,
            self._read_pcm(CaptureSourceId.LOCAL_MIC, plan=plan, sink_name=sink_name),
        )
        remote = self.segment_writer.write_segment(
            CaptureSourceId.SYSTEM_AUDIO,
            self._read_pcm(CaptureSourceId.SYSTEM_AUDIO, plan=plan, sink_name=sink_name),
        )
        return (local, remote)

    def _read_pcm(
        self,
        source_id: CaptureSourceId,
        *,
        plan: CaptureRoutePlan,
        sink_name: str,
    ) -> bytes:
        if self.pcm_provider:
            return self.pcm_provider(source_id)
        return self.pipewire_pcm_provider.read_segment(
            source_id,
            plan=plan,
            sink_name=sink_name,
            segment_seconds=self.segment_seconds,
        )

    def _cleanup(self, *, null_sink_module: str | None, loopback_module: str | None) -> None:
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
        null_sink_module: str,
        loopback_module: str | None,
    ) -> None:
        self._adapter = adapter
        self._plan = plan
        self.sink_name = sink_name
        self.null_sink_module = null_sink_module
        self.loopback_module = loopback_module
        self._segments: list[CaptureSegment] = []
        self._stopped = False

    def flush_segments(self) -> tuple[CaptureSegment, ...]:
        if self._stopped:
            return ()
        segments = self._adapter._flush_once(plan=self._plan, sink_name=self.sink_name)
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
    block_align = channels * sample_width_bytes
    if block_align <= 0:
        raise ValueError("WAV block alignment must be positive.")
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


def _communicate_bounded(
    process: subprocess.Popen[bytes],
    timeout: int,
) -> tuple[bytes, bytes, int]:
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
    returncode = getattr(process, "returncode", 0)
    return stdout, stderr, returncode


def _pulse_sink_input_id(route_id: str) -> str:
    prefix = "pulse:sink-input:"
    if not route_id.startswith(prefix):
        raise ValueError(f"Unsupported Pulse sink-input route id '{route_id}'.")
    sink_input_id = route_id.removeprefix(prefix)
    if not sink_input_id.isdigit():
        raise ValueError(f"Unsupported Pulse sink-input route id '{route_id}'.")
    return sink_input_id
