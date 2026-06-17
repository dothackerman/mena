import io
import subprocess
import threading
import wave
from pathlib import Path
from typing import Any, cast

import pytest

from mena.capture import (
    CAPTURE_MODE_DEDICATED_ROUTE,
    CAPTURE_MODE_SELECTED_MICROPHONE,
    CAPTURE_MODE_SYSTEM_MIX_REMOTE,
    MAX_SEGMENT_BYTES,
    CaptureRouteEvent,
    CaptureRoutePlan,
    CaptureSourceError,
    PipeWireCaptureAdapter,
    PipeWireClient,
    PipeWirePcmProvider,
    SilencePcmProvider,
    WavSegmentWriter,
    make_wav_bytes,
    session_null_sink_name,
)
from mena.profiles import CaptureProfileId, CaptureSourceId


def test_wav_segment_writer_writes_valid_sealed_wav(tmp_path: Path) -> None:
    writer = WavSegmentWriter(segment_dir=tmp_path, sample_rate=8_000)

    segment = writer.write_segment(CaptureSourceId.LOCAL_MIC, b"\x00\x00" * 800)

    assert segment.path.name == "local_mic_000001.wav"
    assert segment.status == "sealed"
    assert segment.sha256
    with wave.open(str(segment.path), "rb") as wav:
        assert wav.getframerate() == 8_000
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getnframes() == 800


def test_wav_segment_writer_refuses_to_overwrite_existing_segment(tmp_path: Path) -> None:
    writer = WavSegmentWriter(segment_dir=tmp_path, sample_rate=8_000)
    source_dir = tmp_path / CaptureSourceId.LOCAL_MIC.value
    source_dir.mkdir()
    existing = source_dir / "local_mic_000001.wav"
    existing.write_bytes(b"already sealed")
    writer._next_index = lambda _source_id, _source_dir: 1  # type: ignore[assignment,method-assign]  # noqa: SLF001

    with pytest.raises(FileExistsError, match="already exists"):
        writer.write_segment(CaptureSourceId.LOCAL_MIC, b"\x00\x00" * 800)

    assert existing.read_bytes() == b"already sealed"


def test_wav_segment_writer_increments_per_source_indexes(tmp_path: Path) -> None:
    writer = WavSegmentWriter(segment_dir=tmp_path)

    local = writer.write_segment(CaptureSourceId.LOCAL_MIC, b"\x00\x00")
    remote_one = writer.write_segment(CaptureSourceId.SYSTEM_AUDIO, b"\x00\x00")
    remote_two = writer.write_segment(CaptureSourceId.SYSTEM_AUDIO, b"\x00\x00")

    assert local.index == 1
    assert remote_one.index == 1
    assert remote_two.index == 2


def test_wav_segment_writer_continues_after_existing_sealed_segment(tmp_path: Path) -> None:
    first_writer = WavSegmentWriter(segment_dir=tmp_path)
    first = first_writer.write_segment(CaptureSourceId.LOCAL_MIC, b"\x00\x00")
    restarted_writer = WavSegmentWriter(segment_dir=tmp_path)

    second = restarted_writer.write_segment(CaptureSourceId.LOCAL_MIC, b"\x00\x00")

    assert first.index == 1
    assert second.index == 2
    assert first.path.exists()
    assert second.path.exists()


def test_make_wav_bytes_rejects_oversized_segments() -> None:
    with pytest.raises(ValueError, match="Segment exceeds"):
        make_wav_bytes(b"\x00" * (MAX_SEGMENT_BYTES + 1))


def test_make_wav_bytes_rejects_unaligned_pcm() -> None:
    with pytest.raises(ValueError, match="complete WAV frames"):
        make_wav_bytes(b"\x00", channels=1, sample_width_bytes=2)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"sample_rate": 0}, "sample_rate"),
        ({"channels": 0}, "channels"),
        ({"sample_width_bytes": 0}, "sample_width_bytes"),
    ],
)
def test_make_wav_bytes_rejects_non_positive_parameters(
    kwargs: dict[str, int],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        make_wav_bytes(b"\x00\x00", **kwargs)


def test_session_null_sink_name_sanitizes_session_id() -> None:
    assert session_null_sink_name("abc-123 / Demo!") == "mena_abc_123___Demo_remote"
    assert session_null_sink_name("!!!") == "mena_session_remote"


def test_pipewire_client_uses_argument_list_commands() -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="77\n", stderr="")

    client = PipeWireClient(runner=runner)
    assert client.create_null_sink("mena_session_remote") == "77"
    client.move_playback_stream("pulse:sink-input:51", "mena_session_remote")
    assert client.create_loopback("mena_session_remote", "alsa_output.usb_headphones") == "77"
    client.unload_module("77")

    assert calls == [
        [
            "pactl",
            "load-module",
            "module-null-sink",
            "sink_name=mena_session_remote",
            "sink_properties=device.description=mena_session_remote",
        ],
        ["pactl", "move-sink-input", "51", "mena_session_remote"],
        [
            "pactl",
            "load-module",
            "module-loopback",
            "source=mena_session_remote.monitor",
            "sink=alsa_output.usb_headphones",
        ],
        ["pactl", "unload-module", "77"],
    ]


def test_pipewire_client_rejects_unsupported_route_id() -> None:
    client = PipeWireClient(runner=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0))

    with pytest.raises(ValueError, match="Unsupported Pulse sink-input route id"):
        client.move_playback_stream("alsa:node:51", "mena_session_remote")


def test_capture_route_event_serializes_null_source() -> None:
    event = CaptureRouteEvent(
        event="degraded",
        code="route_restoration_incomplete",
        message="Route cleanup needs operator review.",
        source_id=None,
        route_id=None,
        route_label=None,
        capture_mode=None,
    )

    assert event.to_dict()["source_id"] is None


def test_pipewire_pcm_provider_rejects_invalid_pipewire_node_targets() -> None:
    provider = PipeWirePcmProvider()

    with pytest.raises(ValueError, match="Unsupported PipeWire route id"):
        provider._command_for_source(  # noqa: SLF001
            CaptureSourceId.LOCAL_MIC,
            plan=CaptureRoutePlan(
                session_id="session-1",
                microphone_route_id="pulse:source:31",
            ),
            sink_name="mena_session_remote",
        )
    with pytest.raises(ValueError, match="Unsupported PipeWire route id"):
        provider._command_for_source(  # noqa: SLF001
            CaptureSourceId.LOCAL_MIC,
            plan=CaptureRoutePlan(
                session_id="session-1",
                microphone_route_id="pipewire:node:not-a-number",
            ),
            sink_name="mena_session_remote",
        )


def test_pipewire_pcm_provider_reads_local_source_from_continuous_pw_record() -> None:
    started_commands: list[list[str]] = []

    class FakeProcess:
        def __init__(self, args: list[str], **_kwargs: object) -> None:
            self.args = args
            self.stdout = io.BytesIO(b"\x01\x02")
            self.stderr = io.BytesIO()
            self.returncode: int | None = None
            started_commands.append(args)

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            return b"", b""

    provider = PipeWirePcmProvider(
        sample_rate=1,
        popen_factory=cast(Any, FakeProcess),
    )

    pcm = provider.read_segment(
        CaptureSourceId.LOCAL_MIC,
        plan=_route_plan(),
        sink_name="mena_session_remote",
        segment_seconds=1,
    )

    assert pcm == b"\x01\x02"
    assert started_commands == [
        [
            "pw-record",
            "--target",
            "31",
            "--raw",
            "--format",
            "s16",
            "--rate",
            "1",
            "--channels",
            "1",
            "-",
        ]
    ]


def test_pipewire_pcm_provider_reads_room_mic_from_selected_microphone() -> None:
    started_commands: list[list[str]] = []

    class FakeProcess:
        def __init__(self, args: list[str], **_kwargs: object) -> None:
            self.args = args
            self.stdout = io.BytesIO(b"\x01\x02")
            self.stderr = io.BytesIO()
            self.returncode: int | None = None
            started_commands.append(args)

        def poll(self) -> int | None:
            return self.returncode

    provider = PipeWirePcmProvider(sample_rate=1, popen_factory=cast(Any, FakeProcess))

    pcm = provider.read_segment(
        CaptureSourceId.ROOM_MIC,
        plan=_in_person_route_plan(),
        sink_name="",
        segment_seconds=1,
    )

    assert pcm == b"\x01\x02"
    assert started_commands[0][:3] == ["pw-record", "--target", "31"]


def test_pipewire_pcm_provider_reuses_continuous_stream_across_segments() -> None:
    started_commands: list[list[str]] = []

    class FakeProcess:
        def __init__(self, args: list[str], **_kwargs: object) -> None:
            self.args = args
            self.stdout = io.BytesIO(b"\x01\x02\x03\x04")
            self.stderr = io.BytesIO()
            self.returncode: int | None = None
            started_commands.append(args)

        def poll(self) -> int | None:
            return self.returncode

    provider = PipeWirePcmProvider(sample_rate=1, popen_factory=cast(Any, FakeProcess))

    first = provider.read_segment(
        CaptureSourceId.SYSTEM_AUDIO,
        plan=_route_plan(),
        sink_name="mena_session_remote",
        segment_seconds=1,
    )
    second = provider.read_segment(
        CaptureSourceId.SYSTEM_AUDIO,
        plan=_route_plan(),
        sink_name="mena_session_remote",
        segment_seconds=1,
    )

    assert first == b"\x01\x02"
    assert second == b"\x03\x04"
    assert len(started_commands) == 1


def test_pipewire_pcm_provider_starts_sources_before_collecting_continuous_chunks() -> None:
    started_commands: list[list[str]] = []
    read_sources: list[list[str]] = []

    class FakeProcess:
        def __init__(self, args: list[str], **_kwargs: object) -> None:
            self.args = args
            self.stdout = io.BytesIO(b"\x00\x00")
            self.stderr = io.BytesIO()
            self.returncode: int | None = None
            started_commands.append(args)

        def poll(self) -> int | None:
            return self.returncode

    class TrackingBytesIO(io.BytesIO):
        def __init__(self, owner: list[str], value: bytes) -> None:
            super().__init__(value)
            self.owner = owner

        def read(self, size: int | None = -1) -> bytes:
            read_sources.append(self.owner)
            return super().read(size)

    provider = PipeWirePcmProvider(
        sample_rate=1,
        popen_factory=cast(Any, FakeProcess),
    )
    original_popen = provider._popen_factory

    def popen(args: list[str], **kwargs: object) -> FakeProcess:
        process = original_popen(args, **kwargs)
        assert isinstance(process, FakeProcess)
        process.stdout = TrackingBytesIO(args, b"\x00\x00")
        return process

    provider._popen_factory = cast(Any, popen)

    pcm = provider.read_aligned_segments(
        plan=_route_plan(),
        sink_name="mena_session_remote",
        segment_seconds=1,
    )

    assert len(started_commands) == 2
    assert read_sources == started_commands
    assert pcm == {
        CaptureSourceId.LOCAL_MIC: b"\x00\x00",
        CaptureSourceId.SYSTEM_AUDIO: b"\x00\x00",
    }


def test_pipewire_pcm_provider_probes_candidate_stream_before_switching() -> None:
    started_commands: list[list[str]] = []
    processes: list[object] = []

    class FakeProcess:
        def __init__(self, args: list[str], **_kwargs: object) -> None:
            self.args = args
            self.stdout = io.BytesIO(b"\x01\x02")
            self.stderr = io.BytesIO()
            self.returncode: int | None = None
            self.terminated = False
            started_commands.append(args)
            processes.append(self)

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            return b"", b""

    provider = PipeWirePcmProvider(sample_rate=1, popen_factory=cast(Any, FakeProcess))

    provider.probe_source(
        CaptureSourceId.SYSTEM_AUDIO,
        plan=_route_plan(),
        sink_name="mena_session_remote",
        timeout_seconds=1,
    )
    provider.close_source(
        CaptureSourceId.SYSTEM_AUDIO,
        plan=_route_plan(),
        sink_name="mena_session_remote",
    )

    assert started_commands[0][:3] == ["pw-record", "--target", "mena_session_remote.monitor"]
    assert cast(Any, processes[0]).terminated


def test_pipewire_pcm_provider_probe_closes_failed_candidate_stream() -> None:
    processes: list[object] = []

    class FakeProcess:
        def __init__(self, args: list[str], **_kwargs: object) -> None:
            self.args = args
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO(b"no frames")
            self.returncode: int | None = None
            self.terminated = False
            processes.append(self)

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            return b"", b""

    provider = PipeWirePcmProvider(sample_rate=1, popen_factory=cast(Any, FakeProcess))

    with pytest.raises(subprocess.CalledProcessError):
        provider.probe_source(
            CaptureSourceId.SYSTEM_AUDIO,
            plan=_route_plan(),
            sink_name="mena_session_remote",
            timeout_seconds=1,
        )

    assert cast(Any, processes[0]).terminated


def test_pipewire_pcm_provider_probe_timeout_is_bounded() -> None:
    processes: list[object] = []

    class FakeProcess:
        def __init__(self, args: list[str], **_kwargs: object) -> None:
            self.args = args
            self.stdout = io.BytesIO(b"\x01\x02")
            self.stderr = io.BytesIO()
            self.returncode: int | None = None
            self.terminated = False
            processes.append(self)

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            return b"", b""

    provider = PipeWirePcmProvider(sample_rate=1, popen_factory=cast(Any, FakeProcess))

    with pytest.raises(subprocess.TimeoutExpired):
        provider.probe_source(
            CaptureSourceId.SYSTEM_AUDIO,
            plan=_route_plan(),
            sink_name="mena_session_remote",
            timeout_seconds=0,
        )

    assert cast(Any, processes[0]).terminated


def test_pipewire_pcm_provider_closes_streams_on_stop_event() -> None:
    class FakeProcess:
        def __init__(self, args: list[str], **_kwargs: object) -> None:
            self.args = args
            self.stdout = io.BytesIO(b"\x00\x00")
            self.stderr = io.BytesIO()
            self.returncode: int | None = None
            self.terminated = False

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            return b"", b""

    provider = PipeWirePcmProvider(
        sample_rate=1,
        popen_factory=cast(Any, FakeProcess),
    )
    stop_event = threading.Event()
    stop_event.set()

    assert (
        provider.read_segment(
            CaptureSourceId.LOCAL_MIC,
            plan=_route_plan(),
            sink_name="mena_session_remote",
            segment_seconds=1,
            stop_event=stop_event,
        )
        == b""
    )


def test_pipewire_pcm_provider_rejects_empty_failed_stream() -> None:
    class FakeProcess:
        def __init__(self, args: list[str], **_kwargs: object) -> None:
            self.args = args
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO(b"capture failed")
            self.returncode = 0

        def poll(self) -> int | None:
            return 1

    provider = PipeWirePcmProvider(
        sample_rate=1,
        popen_factory=cast(Any, FakeProcess),
    )

    with pytest.raises(CaptureSourceError):
        provider.read_aligned_segments(
            plan=_route_plan(),
            sink_name="mena_session_remote",
            segment_seconds=1,
        )


def test_pipewire_pcm_provider_rejects_unexpected_short_chunk() -> None:
    class FakeProcess:
        def __init__(self, args: list[str], **_kwargs: object) -> None:
            self.args = args
            self.stdout = io.BytesIO(b"\x00\x00")
            self.stderr = io.BytesIO(b"route disappeared")
            self.returncode = 1

        def poll(self) -> int | None:
            return self.returncode

    provider = PipeWirePcmProvider(
        sample_rate=2,
        popen_factory=cast(Any, FakeProcess),
    )

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        provider.read_segment(
            CaptureSourceId.LOCAL_MIC,
            plan=_route_plan(),
            sink_name="mena_session_remote",
            segment_seconds=1,
        )

    assert exc_info.value.output == b"\x00\x00"
    assert exc_info.value.stderr == b"route disappeared"


def test_pipewire_pcm_provider_rejects_stream_without_stdout() -> None:
    class FakeProcess:
        def __init__(self, args: list[str], **_kwargs: object) -> None:
            self.args = args
            self.stdout = None
            self.stderr = io.BytesIO()
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

    provider = PipeWirePcmProvider(
        sample_rate=1,
        popen_factory=cast(Any, FakeProcess),
    )

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        provider.read_segment(
            CaptureSourceId.LOCAL_MIC,
            plan=_route_plan(),
            sink_name="mena_session_remote",
            segment_seconds=1,
        )

    assert exc_info.value.stderr == b"pw-record did not expose stdout"


def test_pipewire_pcm_provider_close_kills_stream_after_timeout() -> None:
    class FakeProcess:
        def __init__(self, args: list[str], **_kwargs: object) -> None:
            self.args = args
            self.stdout = io.BytesIO(b"\x00\x00")
            self.stderr = io.BytesIO()
            self.returncode: int | None = None
            self.terminated = False
            self.killed = False

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            if timeout is not None and not self.killed:
                raise subprocess.TimeoutExpired(self.args, timeout)
            return b"", b""

    provider = PipeWirePcmProvider(
        sample_rate=1,
        popen_factory=cast(Any, FakeProcess),
    )

    assert (
        provider.read_segment(
            CaptureSourceId.LOCAL_MIC,
            plan=_route_plan(),
            sink_name="mena_session_remote",
            segment_seconds=1,
        )
        == b"\x00\x00"
    )
    stream = next(iter(provider._streams.values()))
    process = cast(Any, stream.process)

    provider.close()

    assert process.terminated is True
    assert process.killed is True


def test_pipewire_pcm_provider_cleans_up_started_processes_on_spawn_failure() -> None:
    class FakeProcess:
        def __init__(self, args: list[str], **_kwargs: object) -> None:
            self.args = args
            self.stdout = io.BytesIO(b"\x00\x00")
            self.stderr = io.BytesIO()
            self.returncode: int | None = None
            if "--target" in args and "mena_session_remote.monitor" in args:
                raise FileNotFoundError("pw-record")

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            return b"", b""

    provider = PipeWirePcmProvider(
        sample_rate=1,
        popen_factory=cast(Any, FakeProcess),
    )

    with pytest.raises(FileNotFoundError):
        provider.read_aligned_segments(
            plan=_route_plan(),
            sink_name="mena_session_remote",
            segment_seconds=1,
        )


def test_pipewire_capture_adapter_rejects_missing_online_routes(tmp_path: Path) -> None:
    adapter = PipeWireCaptureAdapter(
        client=PipeWireClient(runner=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0)),
        segment_writer=WavSegmentWriter(segment_dir=tmp_path),
    )

    with pytest.raises(ValueError, match="playback stream route"):
        adapter.start(
            CaptureRoutePlan(session_id="session-1", microphone_route_id="pipewire:node:31")
        )
    with pytest.raises(ValueError, match="output sink route"):
        adapter.start(
            CaptureRoutePlan(
                session_id="session-1",
                microphone_route_id="pipewire:node:31",
                playback_stream_route_id="pulse:sink-input:51",
            )
        )


def test_pipewire_capture_adapter_default_flush_uses_aligned_segments(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    module_ids = iter(["101\n", "", "202\n", "", ""])

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=next(module_ids), stderr="")

    class FakeAlignedProvider:
        def read_aligned_segments(
            self,
            *,
            plan: CaptureRoutePlan,
            sink_name: str,
            segment_seconds: int,
            stop_event: threading.Event | None = None,
        ) -> dict[CaptureSourceId, bytes]:
            assert plan == _route_plan()
            assert sink_name == "mena_session_1_remote"
            assert segment_seconds == 30
            assert stop_event is None
            return {
                CaptureSourceId.LOCAL_MIC: b"\x00\x00",
                CaptureSourceId.SYSTEM_AUDIO: b"\x00\x00",
            }

    adapter = PipeWireCaptureAdapter(
        client=PipeWireClient(runner=runner),
        segment_writer=WavSegmentWriter(segment_dir=tmp_path),
    )
    adapter.pipewire_pcm_provider = cast(Any, FakeAlignedProvider())

    active = adapter.start(_route_plan())
    flushed = active.flush_segments()

    assert [segment.source_id for segment in flushed] == [
        CaptureSourceId.LOCAL_MIC,
        CaptureSourceId.SYSTEM_AUDIO,
    ]
    assert calls[:3] == [
        [
            "pactl",
            "load-module",
            "module-null-sink",
            "sink_name=mena_session_1_remote",
            "sink_properties=device.description=mena_session_1_remote",
        ],
        ["pactl", "move-sink-input", "51", "mena_session_1_remote"],
        [
            "pactl",
            "load-module",
            "module-loopback",
            "source=mena_session_1_remote.monitor",
            "sink=alsa_output.usb_headphones",
        ],
    ]


def test_pipewire_pcm_provider_uses_output_sink_monitor_for_system_mix_fallback() -> None:
    provider = PipeWirePcmProvider()
    plan = CaptureRoutePlan(
        session_id="session-1",
        microphone_route_id="pipewire:node:31",
        playback_stream_route_id="pulse:sink-input:51",
        output_sink_route_id="pipewire:node:42",
        system_audio_mode=CAPTURE_MODE_SYSTEM_MIX_REMOTE,
    )

    command = provider._command_for_source(  # noqa: SLF001
        CaptureSourceId.SYSTEM_AUDIO,
        plan=plan,
        sink_name="mena_session_remote",
    )

    assert command[2] == "42.monitor"


def test_pipewire_pcm_provider_rejects_unknown_system_audio_mode() -> None:
    provider = PipeWirePcmProvider()
    plan = CaptureRoutePlan(
        session_id="session-1",
        microphone_route_id="pipewire:node:31",
        playback_stream_route_id="pulse:sink-input:51",
        output_sink_route_id="pipewire:node:42",
        system_audio_mode="mystery",
    )

    with pytest.raises(ValueError, match="Unsupported system audio capture mode"):
        provider._command_for_source(  # noqa: SLF001
            CaptureSourceId.SYSTEM_AUDIO,
            plan=plan,
            sink_name="mena_session_remote",
        )


def test_pipewire_capture_adapter_degrades_and_recovers_at_segment_boundary(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    module_ids = iter(["101\n", "202\n", "", "", "303\n", "404\n"])

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        stdout = next(module_ids, "") if args[:2] == ["pactl", "load-module"] else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    class FallbackProvider:
        def __init__(self) -> None:
            self.modes: list[str] = []
            self.closed = 0
            self.closed_sources: list[tuple[CaptureSourceId, str]] = []

        def read_aligned_segments(
            self,
            *,
            plan: CaptureRoutePlan,
            sink_name: str,
            segment_seconds: int,
            stop_event: threading.Event | None = None,
        ) -> dict[CaptureSourceId, bytes]:
            self.modes.append(plan.system_audio_mode)
            if len(self.modes) == 1:
                raise CaptureSourceError(
                    CaptureSourceId.SYSTEM_AUDIO,
                    RuntimeError("route disappeared"),
                )
            return {
                CaptureSourceId.LOCAL_MIC: b"\x00\x00",
                CaptureSourceId.SYSTEM_AUDIO: b"\x01\x01",
            }

        def probe_source(
            self,
            source_id: CaptureSourceId,
            *,
            plan: CaptureRoutePlan,
            sink_name: str,
            timeout_seconds: float,
        ) -> None:
            self.modes.append(f"probe:{plan.system_audio_mode}")
            assert source_id is CaptureSourceId.SYSTEM_AUDIO
            assert sink_name == "mena_session_1_remote"
            assert timeout_seconds > 0

        def close(self) -> None:
            self.closed += 1

        def close_source(
            self,
            source_id: CaptureSourceId,
            *,
            plan: CaptureRoutePlan,
            sink_name: str,
        ) -> None:
            self.closed_sources.append((source_id, plan.system_audio_mode))

    provider = FallbackProvider()
    adapter = PipeWireCaptureAdapter(
        client=PipeWireClient(runner=runner),
        segment_writer=WavSegmentWriter(segment_dir=tmp_path, sample_rate=1),
    )
    adapter.pipewire_pcm_provider = cast(Any, provider)
    active = adapter.start(_route_plan())

    degraded_segments = active.flush_segments()
    degraded_events = active.drain_route_events()
    recovered_segments = active.flush_segments()
    recovered_events = active.drain_route_events()

    assert [segment.capture_mode for segment in degraded_segments] == [
        CAPTURE_MODE_SELECTED_MICROPHONE,
        CAPTURE_MODE_SYSTEM_MIX_REMOTE,
    ]
    assert degraded_events[0].code == "dedicated_route_runtime_failed"
    assert degraded_events[0].route_id == "pipewire:node:42"
    assert [segment.capture_mode for segment in recovered_segments] == [
        CAPTURE_MODE_SELECTED_MICROPHONE,
        CAPTURE_MODE_DEDICATED_ROUTE,
    ]
    assert recovered_events[0].code == "dedicated_route_recovered"
    assert provider.modes == [
        CAPTURE_MODE_DEDICATED_ROUTE,
        CAPTURE_MODE_SYSTEM_MIX_REMOTE,
        f"probe:{CAPTURE_MODE_DEDICATED_ROUTE}",
        CAPTURE_MODE_DEDICATED_ROUTE,
    ]
    assert provider.closed == 0
    assert provider.closed_sources == [
        (CaptureSourceId.SYSTEM_AUDIO, CAPTURE_MODE_DEDICATED_ROUTE),
        (CaptureSourceId.SYSTEM_AUDIO, CAPTURE_MODE_SYSTEM_MIX_REMOTE),
    ]


def test_pipewire_capture_adapter_preserves_mic_chunk_when_system_audio_degrades(
    tmp_path: Path,
) -> None:
    class FallbackProvider:
        def __init__(self) -> None:
            self.read_segment_calls: list[CaptureSourceId] = []

        def read_aligned_segments(
            self,
            *,
            plan: CaptureRoutePlan,
            sink_name: str,
            segment_seconds: int,
            stop_event: threading.Event | None = None,
        ) -> dict[CaptureSourceId, bytes]:
            raise CaptureSourceError(
                CaptureSourceId.SYSTEM_AUDIO,
                RuntimeError("route disappeared"),
                partial_pcm_by_source={CaptureSourceId.LOCAL_MIC: b"\x00\x00"},
            )

        def read_segment(
            self,
            source_id: CaptureSourceId,
            *,
            plan: CaptureRoutePlan,
            sink_name: str,
            segment_seconds: int,
            stop_event: threading.Event | None = None,
        ) -> bytes:
            self.read_segment_calls.append(source_id)
            assert source_id is CaptureSourceId.SYSTEM_AUDIO
            assert plan.system_audio_mode == CAPTURE_MODE_SYSTEM_MIX_REMOTE
            return b"\x01\x01"

        def close(self) -> None:
            return None

        def close_source(
            self,
            source_id: CaptureSourceId,
            *,
            plan: CaptureRoutePlan,
            sink_name: str,
        ) -> None:
            return None

    provider = FallbackProvider()
    adapter = PipeWireCaptureAdapter(
        client=PipeWireClient(
            runner=lambda args, **_kwargs: subprocess.CompletedProcess(args, 0, stdout="")
        ),
        segment_writer=WavSegmentWriter(segment_dir=tmp_path, sample_rate=1),
    )
    adapter.pipewire_pcm_provider = cast(Any, provider)

    active = adapter.start(_route_plan())
    segments = active.flush_segments()

    assert [segment.source_id for segment in segments] == [
        CaptureSourceId.LOCAL_MIC,
        CaptureSourceId.SYSTEM_AUDIO,
    ]
    assert provider.read_segment_calls == [CaptureSourceId.SYSTEM_AUDIO]


def test_pipewire_capture_adapter_keeps_system_mix_when_recovery_fails(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    module_ids = iter(["101\n", "202\n"])

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["pactl", "move-sink-input"]:
            raise subprocess.CalledProcessError(1, args, stderr="move failed")
        stdout = next(module_ids, "") if args[:2] == ["pactl", "load-module"] else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    class FallbackProvider:
        def __init__(self) -> None:
            self.closed = 0
            self.flushes = 0
            self.probe_attempts = 0

        def read_aligned_segments(
            self,
            *,
            plan: CaptureRoutePlan,
            sink_name: str,
            segment_seconds: int,
            stop_event: threading.Event | None = None,
        ) -> dict[CaptureSourceId, bytes]:
            assert plan.system_audio_mode == CAPTURE_MODE_SYSTEM_MIX_REMOTE
            self.flushes += 1
            return {
                CaptureSourceId.LOCAL_MIC: b"\x00\x00",
                CaptureSourceId.SYSTEM_AUDIO: b"\x01\x01",
            }

        def probe_source(
            self,
            source_id: CaptureSourceId,
            *,
            plan: CaptureRoutePlan,
            sink_name: str,
            timeout_seconds: float,
        ) -> None:
            self.probe_attempts += 1

        def close(self) -> None:
            self.closed += 1

    provider = FallbackProvider()
    adapter = PipeWireCaptureAdapter(
        client=PipeWireClient(runner=runner),
        segment_writer=WavSegmentWriter(segment_dir=tmp_path, sample_rate=1),
    )
    adapter.pipewire_pcm_provider = cast(Any, provider)
    active = adapter.start(_route_plan())
    setup_events = active.drain_route_events()

    first = active.flush_segments()
    second = active.flush_segments()
    third = active.flush_segments()
    recovery_events = active.drain_route_events()

    assert setup_events[0].code == "dedicated_route_unavailable"
    assert first[1].capture_mode == CAPTURE_MODE_SYSTEM_MIX_REMOTE
    assert second[1].capture_mode == CAPTURE_MODE_SYSTEM_MIX_REMOTE
    assert third[1].capture_mode == CAPTURE_MODE_SYSTEM_MIX_REMOTE
    assert recovery_events == ()
    assert provider.flushes == 3
    assert provider.probe_attempts == 0
    assert provider.closed == 0


def test_pipewire_capture_adapter_keeps_system_mix_when_recovery_probe_fails(
    tmp_path: Path,
) -> None:
    module_ids = iter(["101\n", "202\n", "303\n", "404\n", "505\n", "606\n"])
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        stdout = next(module_ids, "") if args[:2] == ["pactl", "load-module"] else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    class FallbackProvider:
        def __init__(self) -> None:
            self.modes: list[str] = []
            self.closed = 0
            self.closed_sources: list[tuple[CaptureSourceId, str]] = []
            self.probe_attempts = 0

        def read_aligned_segments(
            self,
            *,
            plan: CaptureRoutePlan,
            sink_name: str,
            segment_seconds: int,
            stop_event: threading.Event | None = None,
        ) -> dict[CaptureSourceId, bytes]:
            self.modes.append(plan.system_audio_mode)
            if len(self.modes) == 1:
                raise CaptureSourceError(
                    CaptureSourceId.SYSTEM_AUDIO,
                    RuntimeError("route disappeared"),
                )
            return {
                CaptureSourceId.LOCAL_MIC: b"\x00\x00",
                CaptureSourceId.SYSTEM_AUDIO: b"\x01\x01",
            }

        def probe_source(
            self,
            source_id: CaptureSourceId,
            *,
            plan: CaptureRoutePlan,
            sink_name: str,
            timeout_seconds: float,
        ) -> None:
            self.probe_attempts += 1
            raise subprocess.TimeoutExpired(["pw-record"], timeout_seconds)

        def close(self) -> None:
            self.closed += 1

        def close_source(
            self,
            source_id: CaptureSourceId,
            *,
            plan: CaptureRoutePlan,
            sink_name: str,
        ) -> None:
            self.closed_sources.append((source_id, plan.system_audio_mode))

    provider = FallbackProvider()
    adapter = PipeWireCaptureAdapter(
        client=PipeWireClient(runner=runner),
        segment_writer=WavSegmentWriter(segment_dir=tmp_path, sample_rate=1),
    )
    adapter.pipewire_pcm_provider = cast(Any, provider)
    active = adapter.start(_route_plan())

    degraded_segments = active.flush_segments()
    active.drain_route_events()
    still_degraded_segments = active.flush_segments()
    recovery_events = active.drain_route_events()
    backoff_segments = active.flush_segments()

    assert degraded_segments[1].capture_mode == CAPTURE_MODE_SYSTEM_MIX_REMOTE
    assert still_degraded_segments[1].capture_mode == CAPTURE_MODE_SYSTEM_MIX_REMOTE
    assert backoff_segments[1].capture_mode == CAPTURE_MODE_SYSTEM_MIX_REMOTE
    assert recovery_events == ()
    assert provider.closed == 0
    assert provider.probe_attempts == 1
    assert provider.closed_sources == [(CaptureSourceId.SYSTEM_AUDIO, CAPTURE_MODE_DEDICATED_ROUTE)]


def test_pipewire_capture_adapter_keeps_system_mix_when_recovered_segment_fails(
    tmp_path: Path,
) -> None:
    module_ids = iter(["101\n", "202\n", "303\n", "404\n", "505\n", "606\n"])

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = next(module_ids, "") if args[:2] == ["pactl", "load-module"] else ""
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    class FallbackProvider:
        def __init__(self) -> None:
            self.dedicated_reads = 0
            self.probe_attempts = 0
            self.closed = 0
            self.closed_sources: list[tuple[CaptureSourceId, str]] = []

        def read_aligned_segments(
            self,
            *,
            plan: CaptureRoutePlan,
            sink_name: str,
            segment_seconds: int,
            stop_event: threading.Event | None = None,
        ) -> dict[CaptureSourceId, bytes]:
            if plan.system_audio_mode == CAPTURE_MODE_DEDICATED_ROUTE:
                self.dedicated_reads += 1
                raise CaptureSourceError(
                    CaptureSourceId.SYSTEM_AUDIO,
                    RuntimeError("dedicated stream failed"),
                )
            return {
                CaptureSourceId.LOCAL_MIC: b"\x00\x00",
                CaptureSourceId.SYSTEM_AUDIO: b"\x01\x01",
            }

        def probe_source(
            self,
            source_id: CaptureSourceId,
            *,
            plan: CaptureRoutePlan,
            sink_name: str,
            timeout_seconds: float,
        ) -> None:
            self.probe_attempts += 1

        def close(self) -> None:
            self.closed += 1

        def close_source(
            self,
            source_id: CaptureSourceId,
            *,
            plan: CaptureRoutePlan,
            sink_name: str,
        ) -> None:
            self.closed_sources.append((source_id, plan.system_audio_mode))

    provider = FallbackProvider()
    adapter = PipeWireCaptureAdapter(
        client=PipeWireClient(runner=runner),
        segment_writer=WavSegmentWriter(segment_dir=tmp_path, sample_rate=1),
    )
    adapter.pipewire_pcm_provider = cast(Any, provider)
    active = adapter.start(_route_plan())

    degraded_segments = active.flush_segments()
    first_events = active.drain_route_events()
    still_degraded_segments = active.flush_segments()
    second_events = active.drain_route_events()
    backoff_segments = active.flush_segments()
    backoff_events = active.drain_route_events()

    assert degraded_segments[1].capture_mode == CAPTURE_MODE_SYSTEM_MIX_REMOTE
    assert first_events[0].event == "degraded"
    assert still_degraded_segments[1].capture_mode == CAPTURE_MODE_SYSTEM_MIX_REMOTE
    assert [event.event for event in second_events] == ["degraded"]
    assert backoff_segments[1].capture_mode == CAPTURE_MODE_SYSTEM_MIX_REMOTE
    assert backoff_events == ()
    assert provider.probe_attempts == 1
    assert provider.dedicated_reads == 2
    assert provider.closed == 0
    assert provider.closed_sources == [
        (CaptureSourceId.SYSTEM_AUDIO, CAPTURE_MODE_DEDICATED_ROUTE),
        (CaptureSourceId.SYSTEM_AUDIO, CAPTURE_MODE_DEDICATED_ROUTE),
    ]


def test_pipewire_capture_adapter_flushes_segments_and_cleans_up(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    module_ids = iter(["101\n", "", "202\n", "", ""])

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=next(module_ids), stderr="")

    writer = WavSegmentWriter(segment_dir=tmp_path, sample_rate=8_000)
    provider = SilencePcmProvider(sample_rate=8_000, seconds=1).read_segment
    adapter = PipeWireCaptureAdapter(
        client=PipeWireClient(runner=runner),
        segment_writer=writer,
        pcm_provider=provider,
    )
    active = adapter.start(_route_plan())

    flushed = active.flush_segments()
    metadata = active.stop()
    second_stop = active.stop()

    assert active.sink_name == "mena_session_1_remote"
    assert [segment.source_id for segment in flushed] == [
        CaptureSourceId.LOCAL_MIC,
        CaptureSourceId.SYSTEM_AUDIO,
    ]
    assert metadata.to_dict()["segment_count"] == 2
    assert second_stop == metadata
    assert calls[-2:] == [["pactl", "unload-module", "202"], ["pactl", "unload-module", "101"]]
    assert all(segment.path.exists() for segment in metadata.segments)


def test_active_pipewire_capture_returns_no_segments_after_stop(tmp_path: Path) -> None:
    writer = WavSegmentWriter(segment_dir=tmp_path, sample_rate=8_000)
    provider = SilencePcmProvider(sample_rate=8_000, seconds=1).read_segment
    adapter = PipeWireCaptureAdapter(
        client=PipeWireClient(runner=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0)),
        segment_writer=writer,
        pcm_provider=provider,
    )
    active = adapter.start(_in_person_route_plan())

    active.stop()

    assert active.flush_segments() == ()


def test_pipewire_capture_adapter_flushes_in_person_room_mic_without_routing(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    writer = WavSegmentWriter(segment_dir=tmp_path, sample_rate=8_000)
    provider = SilencePcmProvider(sample_rate=8_000, seconds=1).read_segment
    adapter = PipeWireCaptureAdapter(
        client=PipeWireClient(runner=runner),
        segment_writer=writer,
        pcm_provider=provider,
    )
    active = adapter.start(_in_person_route_plan())

    flushed = active.flush_segments()
    metadata = active.stop()

    assert calls == []
    assert [segment.source_id for segment in flushed] == [CaptureSourceId.ROOM_MIC]
    assert metadata.profile_id is CaptureProfileId.IN_PERSON_SINGLE_MIC
    assert metadata.to_dict()["segment_count"] == 1


def test_pipewire_capture_adapter_skips_empty_in_person_stop_segment(
    tmp_path: Path,
) -> None:
    adapter = PipeWireCaptureAdapter(
        client=PipeWireClient(runner=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0)),
        segment_writer=WavSegmentWriter(segment_dir=tmp_path),
        pcm_provider=lambda _source_id: b"",
    )
    active = adapter.start(_in_person_route_plan())

    flushed = active.flush_segments(stop_event=threading.Event())
    metadata = active.stop()

    assert flushed == ()
    assert metadata.segments == ()
    assert not (tmp_path / CaptureSourceId.ROOM_MIC.value).exists()


def test_pipewire_capture_adapter_stop_cleans_up_without_starting_flush(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    module_ids = iter(["101\n", "", "202\n", "", ""])

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=next(module_ids), stderr="")

    def failing_provider(_source_id: CaptureSourceId) -> bytes:
        raise subprocess.CalledProcessError(1, ["pw-record"], stderr="capture failed")

    adapter = PipeWireCaptureAdapter(
        client=PipeWireClient(runner=runner),
        segment_writer=WavSegmentWriter(segment_dir=tmp_path),
        pcm_provider=failing_provider,
    )
    active = adapter.start(_route_plan())

    metadata = active.stop()

    assert metadata.segments == ()
    assert calls[-2:] == [["pactl", "unload-module", "202"], ["pactl", "unload-module", "101"]]


def test_pipewire_capture_adapter_degrades_on_partial_setup_failure(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["pactl", "move-sink-input"]:
            raise subprocess.CalledProcessError(1, args, stderr="move failed")
        return subprocess.CompletedProcess(args, 0, stdout="101\n", stderr="")

    adapter = PipeWireCaptureAdapter(
        client=PipeWireClient(runner=runner),
        segment_writer=WavSegmentWriter(segment_dir=tmp_path),
    )

    active = adapter.start(_route_plan())
    route_events = active.drain_route_events()

    assert calls[-1] == ["pactl", "unload-module", "101"]
    assert route_events[0].event == "degraded"
    assert route_events[0].code == "dedicated_route_unavailable"
    assert route_events[0].capture_mode == CAPTURE_MODE_SYSTEM_MIX_REMOTE


def test_pipewire_capture_adapter_degrades_when_cleanup_after_setup_failure_fails(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["pactl", "move-sink-input"]:
            raise subprocess.CalledProcessError(1, args, stderr="move failed")
        if args[:2] == ["pactl", "unload-module"]:
            raise FileNotFoundError("pactl")
        return subprocess.CompletedProcess(args, 0, stdout="101\n", stderr="")

    adapter = PipeWireCaptureAdapter(
        client=PipeWireClient(runner=runner),
        segment_writer=WavSegmentWriter(segment_dir=tmp_path),
    )

    active = adapter.start(_route_plan())
    route_events = active.drain_route_events()

    assert calls[-1] == ["pactl", "unload-module", "101"]
    assert route_events[0].event == "degraded"


def test_active_pipewire_capture_stop_does_not_start_first_segment(tmp_path: Path) -> None:
    class CountingProvider:
        def __init__(self) -> None:
            self.flushes = 0

        def read_aligned_segments(
            self,
            *,
            plan: CaptureRoutePlan,
            sink_name: str,
            segment_seconds: int,
            stop_event: threading.Event | None = None,
        ) -> dict[CaptureSourceId, bytes]:
            self.flushes += 1
            return {
                CaptureSourceId.LOCAL_MIC: b"\x00\x00",
                CaptureSourceId.SYSTEM_AUDIO: b"\x01\x01",
            }

        def close(self) -> None:
            return None

    provider = CountingProvider()
    adapter = PipeWireCaptureAdapter(
        client=PipeWireClient(runner=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0)),
        segment_writer=WavSegmentWriter(segment_dir=tmp_path, sample_rate=1),
    )
    adapter.pipewire_pcm_provider = cast(Any, provider)

    metadata = adapter.start(_route_plan()).stop()

    assert metadata.segments == ()
    assert provider.flushes == 0


def _route_plan() -> CaptureRoutePlan:
    return CaptureRoutePlan(
        session_id="session-1",
        microphone_route_id="pipewire:node:31",
        playback_stream_route_id="pulse:sink-input:51",
        output_sink_route_id="pipewire:node:42",
        output_sink_backend_name="alsa_output.usb_headphones",
    )


def _in_person_route_plan() -> CaptureRoutePlan:
    return CaptureRoutePlan(
        session_id="session-1",
        microphone_route_id="pipewire:node:31",
        profile_id=CaptureProfileId.IN_PERSON_SINGLE_MIC,
    )
