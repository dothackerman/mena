import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from mena.discovery import (
    AudioDiscovery,
    DiscoveryStatus,
    discover_audio_routes,
    format_discovery,
)
from mena.profiles import CaptureProfileId


def test_discover_online_profile_parses_sources_streams_and_sinks() -> None:
    discovery = discover_audio_routes(
        CaptureProfileId.ONLINE_DUAL_STREAM,
        runner=_runner_with_stdout(_fixture("mixed-audio-graph.json")),
    )

    assert discovery.status is DiscoveryStatus.AVAILABLE
    assert [source.route_id for source in discovery.sources] == ["pipewire:node:31"]
    assert discovery.sources[0].name == "alsa_input.usb-headset"
    assert discovery.sources[0].active is True
    assert [sink.route_id for sink in discovery.sinks] == ["pipewire:node:42"]
    assert discovery.sinks[0].active is False
    assert [stream.route_id for stream in discovery.playback_streams] == ["pulse:sink-input:151"]
    assert [stream.app_binary_name for stream in discovery.playback_streams] == ["firefox"]
    assert discovery.playback_streams[0].media_name == "Meet"
    assert discovery.playback_streams[0].sink_input_id == "151"


def test_discover_in_person_profile_only_returns_microphones() -> None:
    discovery = discover_audio_routes(
        CaptureProfileId.IN_PERSON_SINGLE_MIC,
        runner=_runner_with_stdout(_fixture("mixed-audio-graph.json")),
    )

    assert len(discovery.sources) == 1
    assert discovery.sinks == ()
    assert discovery.playback_streams == ()


def test_discovery_round_trips_through_session_json_dict() -> None:
    discovery = discover_audio_routes(
        CaptureProfileId.ONLINE_DUAL_STREAM,
        runner=_runner_with_stdout(_fixture("mixed-audio-graph.json")),
    )

    restored = AudioDiscovery.from_dict(discovery.to_dict())

    assert restored == discovery


def test_discover_sorts_routes_for_stable_output() -> None:
    graph = """[
      {
        "id": 42,
        "info": {
          "state": "running",
          "props": {
            "media.class": "Audio/Source",
            "node.name": "second"
          }
        }
      },
      {
        "id": 7,
        "info": {
          "state": "running",
          "props": {
            "media.class": "Audio/Source",
            "node.name": "first"
          }
        }
      }
    ]"""

    discovery = discover_audio_routes(
        CaptureProfileId.ONLINE_DUAL_STREAM,
        runner=_runner_with_stdout(graph),
    )

    assert [source.id for source in discovery.sources] == [7, 42]


def test_discover_handles_missing_pipewire_command() -> None:
    def missing_runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    discovery = discover_audio_routes(CaptureProfileId.ONLINE_DUAL_STREAM, runner=missing_runner)

    assert discovery.status is DiscoveryStatus.UNAVAILABLE
    assert discovery.error == "PipeWire command 'pw-dump' was not found."


def test_discover_handles_empty_graph() -> None:
    discovery = discover_audio_routes(
        CaptureProfileId.ONLINE_DUAL_STREAM,
        runner=_runner_with_stdout("[]"),
    )

    assert discovery.status is DiscoveryStatus.AVAILABLE
    assert discovery.sources == ()
    assert discovery.sinks == ()
    assert discovery.playback_streams == ()


def test_discover_handles_invalid_json() -> None:
    discovery = discover_audio_routes(
        CaptureProfileId.ONLINE_DUAL_STREAM,
        runner=_runner_with_stdout("{"),
    )

    assert discovery.status is DiscoveryStatus.UNAVAILABLE
    assert discovery.error
    assert "invalid JSON" in discovery.error


def test_discover_handles_pw_dump_failure() -> None:
    def failing_runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, ["pw-dump"], stderr="permission denied")

    discovery = discover_audio_routes(CaptureProfileId.ONLINE_DUAL_STREAM, runner=failing_runner)

    assert discovery.status is DiscoveryStatus.UNAVAILABLE
    assert discovery.error == "PipeWire discovery failed: permission denied"


def test_format_discovery_prints_human_readable_online_routes() -> None:
    discovery = discover_audio_routes(
        CaptureProfileId.ONLINE_DUAL_STREAM,
        runner=_runner_with_stdout(_fixture("mixed-audio-graph.json")),
    )

    output = format_discovery(discovery)

    assert "Microphones:" in output
    assert "USB Headset Microphone" in output
    assert "Playback streams:" in output
    assert "firefox" in output
    assert "Sinks:" in output


def test_audio_discovery_rejects_invalid_round_trip_shape() -> None:
    discovery_data = discover_audio_routes(
        CaptureProfileId.ONLINE_DUAL_STREAM,
        runner=_runner_with_stdout(_fixture("mixed-audio-graph.json")),
    ).to_dict()
    discovery_data["sources"] = [None]

    with pytest.raises(ValueError, match="discovery item must be an object"):
        AudioDiscovery.from_dict(discovery_data)


def _fixture(name: str) -> str:
    return (Path(__file__).parents[1] / "fixtures" / "pw-dump" / name).read_text(encoding="utf-8")


def _runner_with_stdout(stdout: str) -> Callable[..., subprocess.CompletedProcess[str]]:
    def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["pw-dump"], 0, stdout=stdout, stderr="")

    return runner
