import pytest

from mena.capture_planning import (
    CapturePlanningError,
    RouteSelections,
    build_start_capture_payload,
)
from mena.discovery import AudioDiscovery, AudioRoute, DiscoveryStatus, PlaybackStream
from mena.profiles import CaptureProfileId


def test_build_online_start_payload_binds_selected_routes() -> None:
    payload = build_start_capture_payload(
        profile=CaptureProfileId.ONLINE_DUAL_STREAM,
        language="en",
        discovery=_discovery(CaptureProfileId.ONLINE_DUAL_STREAM),
        selections=RouteSelections(
            microphone_route_id="pipewire:node:31",
            playback_stream_route_id="pulse:sink-input:151",
            output_sink_route_id="pipewire:node:42",
        ),
    )

    assert payload.profile == "online_dual_stream"
    assert payload.language == "en"
    assert payload.selected_sources == ("local_mic", "system_audio")
    assert [route.route_id for route in payload.routes] == [
        "pipewire:node:31",
        "pulse:sink-input:151",
        "pipewire:node:42",
    ]
    assert payload.routes[1].label == "Meet - firefox"
    assert payload.routes[2].backend_name == "Speakers"


def test_build_in_person_start_payload_uses_room_mic_only() -> None:
    payload = build_start_capture_payload(
        profile=CaptureProfileId.IN_PERSON_SINGLE_MIC,
        language="de-CH",
        discovery=_discovery(CaptureProfileId.IN_PERSON_SINGLE_MIC),
        selections=RouteSelections(microphone_route_id="pipewire:node:31"),
    )

    assert payload.profile == "in_person_single_mic"
    assert payload.selected_sources == ("room_mic",)
    assert len(payload.routes) == 1
    assert payload.routes[0].source_id == "room_mic"


def test_build_in_person_payload_rejects_online_routes() -> None:
    with pytest.raises(CapturePlanningError, match="microphone route only"):
        build_start_capture_payload(
            profile=CaptureProfileId.IN_PERSON_SINGLE_MIC,
            language="en",
            discovery=_discovery(CaptureProfileId.IN_PERSON_SINGLE_MIC),
            selections=RouteSelections(
                microphone_route_id="pipewire:node:31",
                playback_stream_route_id="pulse:sink-input:151",
            ),
        )


def test_build_payload_rejects_wrong_profile_discovery() -> None:
    with pytest.raises(CapturePlanningError, match="does not match selected profile"):
        build_start_capture_payload(
            profile=CaptureProfileId.ONLINE_DUAL_STREAM,
            language="en",
            discovery=_discovery(CaptureProfileId.IN_PERSON_SINGLE_MIC),
            selections=RouteSelections(
                microphone_route_id="pipewire:node:31",
                playback_stream_route_id="pulse:sink-input:151",
                output_sink_route_id="pipewire:node:42",
            ),
        )


def test_build_payload_rejects_unknown_route() -> None:
    with pytest.raises(CapturePlanningError, match="was not discovered"):
        build_start_capture_payload(
            profile=CaptureProfileId.ONLINE_DUAL_STREAM,
            language="en",
            discovery=_discovery(CaptureProfileId.ONLINE_DUAL_STREAM),
            selections=RouteSelections(
                microphone_route_id="pipewire:node:999",
                playback_stream_route_id="pulse:sink-input:151",
                output_sink_route_id="pipewire:node:42",
            ),
        )


def test_build_payload_rejects_non_pipewire_microphone_route() -> None:
    with pytest.raises(CapturePlanningError, match="Selected microphone route"):
        build_start_capture_payload(
            profile=CaptureProfileId.IN_PERSON_SINGLE_MIC,
            language="en",
            discovery=_discovery(
                CaptureProfileId.IN_PERSON_SINGLE_MIC,
                microphone_route_id="pulse:source:31",
            ),
            selections=RouteSelections(microphone_route_id="pulse:source:31"),
        )


def test_build_online_payload_rejects_non_pulse_playback_stream_route() -> None:
    with pytest.raises(CapturePlanningError, match="Selected playback stream route"):
        build_start_capture_payload(
            profile=CaptureProfileId.ONLINE_DUAL_STREAM,
            language="en",
            discovery=_discovery(
                CaptureProfileId.ONLINE_DUAL_STREAM,
                playback_stream_route_id="pipewire:node:51",
            ),
            selections=RouteSelections(
                microphone_route_id="pipewire:node:31",
                playback_stream_route_id="pipewire:node:51",
                output_sink_route_id="pipewire:node:42",
            ),
        )


def test_build_online_payload_rejects_non_pipewire_output_sink_route() -> None:
    with pytest.raises(CapturePlanningError, match="Selected output sink route"):
        build_start_capture_payload(
            profile=CaptureProfileId.ONLINE_DUAL_STREAM,
            language="en",
            discovery=_discovery(
                CaptureProfileId.ONLINE_DUAL_STREAM,
                output_sink_route_id="pulse:sink:42",
            ),
            selections=RouteSelections(
                microphone_route_id="pipewire:node:31",
                playback_stream_route_id="pulse:sink-input:151",
                output_sink_route_id="pulse:sink:42",
            ),
        )


def _discovery(
    profile: CaptureProfileId,
    *,
    microphone_route_id: str = "pipewire:node:31",
    playback_stream_route_id: str = "pulse:sink-input:151",
    output_sink_route_id: str = "pipewire:node:42",
) -> AudioDiscovery:
    return AudioDiscovery(
        version=1,
        profile=profile,
        status=DiscoveryStatus.AVAILABLE,
        sources=(
            AudioRoute(
                id=31,
                route_id=microphone_route_id,
                name="Headset Mic",
                description=None,
                media_class="Audio/Source",
                active=True,
            ),
        ),
        sinks=(
            AudioRoute(
                id=42,
                route_id=output_sink_route_id,
                name="Speakers",
                description=None,
                media_class="Audio/Sink",
                active=True,
            ),
        ),
        playback_streams=(
            PlaybackStream(
                id=51,
                route_id=playback_stream_route_id,
                name="Meeting Stream",
                app_binary_name="firefox",
                stream_name="WebRTC playback",
                media_name="Meet",
                sink_input_id="151",
                active=True,
            ),
        ),
    )
