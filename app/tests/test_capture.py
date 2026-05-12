import subprocess
import wave
from pathlib import Path

import pytest

from mena.capture import (
    MAX_SEGMENT_BYTES,
    CaptureRoutePlan,
    PipeWireCaptureAdapter,
    PipeWireClient,
    SilencePcmProvider,
    WavSegmentWriter,
    make_wav_bytes,
    session_null_sink_name,
)
from mena.profiles import CaptureSourceId


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


def test_make_wav_bytes_rejects_oversized_segments() -> None:
    with pytest.raises(ValueError, match="Segment exceeds"):
        make_wav_bytes(b"\x00" * (MAX_SEGMENT_BYTES + 1))


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
    client.move_playback_stream("pipewire:node:51", "mena_session_remote")
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

    with pytest.raises(ValueError, match="Unsupported PipeWire route id"):
        client.move_playback_stream("alsa:node:51", "mena_session_remote")


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


def _route_plan() -> CaptureRoutePlan:
    return CaptureRoutePlan(
        session_id="session-1",
        microphone_route_id="pipewire:node:31",
        playback_stream_route_id="pipewire:node:51",
        output_sink_route_id="pipewire:node:42",
    )
