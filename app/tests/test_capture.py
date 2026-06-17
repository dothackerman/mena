import io
import subprocess
import threading
import wave
from pathlib import Path
from typing import Any, cast

import pytest

from mena.capture import (
    MAX_SEGMENT_BYTES,
    CaptureRoutePlan,
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
    assert client.create_loopback("mena_session_remote", "pipewire:node:42") == "77"
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
            "sink=42",
        ],
        ["pactl", "unload-module", "77"],
    ]


def test_pipewire_client_rejects_unsupported_route_id() -> None:
    client = PipeWireClient(runner=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0))

    with pytest.raises(ValueError, match="Unsupported Pulse sink-input route id"):
        client.move_playback_stream("alsa:node:51", "mena_session_remote")


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

    with pytest.raises(subprocess.CalledProcessError):
        provider.read_aligned_segments(
            plan=_route_plan(),
            sink_name="mena_session_remote",
            segment_seconds=1,
        )


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
            "sink=42",
        ],
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


def test_pipewire_capture_adapter_cleans_up_when_stop_flush_fails(tmp_path: Path) -> None:
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

    with pytest.raises(subprocess.CalledProcessError):
        active.stop()

    assert calls[-2:] == [["pactl", "unload-module", "202"], ["pactl", "unload-module", "101"]]


def test_pipewire_capture_adapter_cleans_up_partial_setup_on_failure(tmp_path: Path) -> None:
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

    with pytest.raises(subprocess.CalledProcessError):
        adapter.start(_route_plan())

    assert calls[-1] == ["pactl", "unload-module", "101"]


def test_pipewire_capture_adapter_preserves_setup_failure_when_cleanup_fails(
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

    with pytest.raises(subprocess.CalledProcessError, match="move-sink-input"):
        adapter.start(_route_plan())

    assert calls[-1] == ["pactl", "unload-module", "101"]


def _route_plan() -> CaptureRoutePlan:
    return CaptureRoutePlan(
        session_id="session-1",
        microphone_route_id="pipewire:node:31",
        playback_stream_route_id="pulse:sink-input:51",
        output_sink_route_id="pipewire:node:42",
    )


def _in_person_route_plan() -> CaptureRoutePlan:
    return CaptureRoutePlan(
        session_id="session-1",
        microphone_route_id="pipewire:node:31",
        profile_id=CaptureProfileId.IN_PERSON_SINGLE_MIC,
    )
