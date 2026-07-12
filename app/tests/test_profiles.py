import pytest

import mena.profiles as profiles_module
from mena.profiles import (
    CaptureProfile,
    CaptureProfileId,
    CaptureSourceId,
    ChannelLayout,
    DeviceRoute,
    DiarisationPolicy,
    get_capture_profile,
    parse_capture_profile,
    profile_for_selection,
)


def test_online_profile_has_expected_sources() -> None:
    profile = get_capture_profile(CaptureProfileId.ONLINE_DUAL_STREAM)

    assert profile.name is CaptureProfileId.ONLINE_DUAL_STREAM
    assert [source.logical_id for source in profile.sources] == [
        CaptureSourceId.LOCAL_MIC,
        CaptureSourceId.SYSTEM_AUDIO,
    ]
    assert [source.channel_layout for source in profile.sources] == [
        ChannelLayout.MONO,
        ChannelLayout.MONO,
    ]
    assert [source.diarisation_policy for source in profile.sources] == [
        DiarisationPolicy.NONE,
        DiarisationPolicy.WHOLE_STREAM,
    ]


def test_in_person_profile_has_room_mic_source() -> None:
    profile = get_capture_profile(CaptureProfileId.IN_PERSON_SINGLE_MIC)

    assert [source.logical_id for source in profile.sources] == [CaptureSourceId.ROOM_MIC]
    assert profile.sources[0].channel_layout is ChannelLayout.MONO
    assert profile.sources[0].diarisation_policy is DiarisationPolicy.WHOLE_STREAM


def test_capture_profile_round_trips_through_json_dict() -> None:
    profile = get_capture_profile(CaptureProfileId.ONLINE_DUAL_STREAM)

    restored = CaptureProfile.from_dict(profile.to_dict())

    assert restored == profile


def test_capture_profile_accepts_discovered_device_routes() -> None:
    profile_data = get_capture_profile(CaptureProfileId.ONLINE_DUAL_STREAM).to_dict()
    sources = profile_data["sources"]
    assert isinstance(sources, list)
    first_source = sources[0]
    assert isinstance(first_source, dict)
    first_source["device_route"] = {
        "backend": "pipewire",
        "route_id": "pipewire:node:31",
        "label": "Headset Mic",
        "metadata": {"node.name": "alsa_input.usb"},
    }

    restored = CaptureProfile.from_dict(profile_data)

    assert restored.sources[0].device_route == DeviceRoute(
        backend="pipewire",
        route_id="pipewire:node:31",
        label="Headset Mic",
        metadata={"node.name": "alsa_input.usb"},
    )


def test_capture_profile_rejects_unknown_name() -> None:
    profile_data = get_capture_profile(CaptureProfileId.ONLINE_DUAL_STREAM).to_dict()
    profile_data["name"] = "boardroom_array"

    with pytest.raises(ValueError, match="Unknown capture profile.name 'boardroom_array'"):
        CaptureProfile.from_dict(profile_data)


def test_capture_profile_rejects_unknown_source_role() -> None:
    profile_data = get_capture_profile(CaptureProfileId.ONLINE_DUAL_STREAM).to_dict()
    sources = profile_data["sources"]
    assert isinstance(sources, list)
    first_source = sources[0]
    assert isinstance(first_source, dict)
    first_source["logical_id"] = "remote_system"

    with pytest.raises(ValueError, match="Unknown capture source.logical_id 'remote_system'"):
        CaptureProfile.from_dict(profile_data)


def test_cli_selection_preserves_durable_profile_id() -> None:
    profile = profile_for_selection(parse_capture_profile("online"))

    assert profile.name is CaptureProfileId.ONLINE_DUAL_STREAM


def test_device_route_round_trips_through_json_dict() -> None:
    route = DeviceRoute(
        backend="pipewire",
        route_id="node:42",
        label="Headset",
        metadata={"node.name": "alsa_input.usb"},
    )

    assert DeviceRoute.from_dict(route.to_dict()) == route


def test_device_route_rejects_invalid_metadata() -> None:
    with pytest.raises(ValueError, match="device_route.metadata"):
        DeviceRoute.from_dict(
            {
                "backend": "pipewire",
                "route_id": "node:42",
                "label": None,
                "metadata": {"node.id": 42},
            }
        )


def test_capture_source_rejects_invalid_device_route() -> None:
    source_data = get_capture_profile(CaptureProfileId.ONLINE_DUAL_STREAM).sources[0].to_dict()
    source_data["device_route"] = "node:42"

    with pytest.raises(ValueError, match="device_route must be an object or null"):
        CaptureProfile.from_dict(
            {
                "name": "online_dual_stream",
                "cli_name": "online",
                "sources": [
                    source_data,
                    get_capture_profile(CaptureProfileId.ONLINE_DUAL_STREAM).sources[1].to_dict(),
                ],
                "metadata": {"routing_schema": "pipewire_dual_stream.v1"},
            }
        )


def test_capture_source_rejects_empty_device_route() -> None:
    source_data = get_capture_profile(CaptureProfileId.ONLINE_DUAL_STREAM).sources[0].to_dict()
    source_data["device_route"] = {}

    with pytest.raises(
        ValueError,
        match="device_route field 'backend' is required and must be a non-empty string.",
    ):
        CaptureProfile.from_dict(
            {
                "name": "online_dual_stream",
                "cli_name": "online",
                "sources": [
                    source_data,
                    get_capture_profile(CaptureProfileId.ONLINE_DUAL_STREAM).sources[1].to_dict(),
                ],
                "metadata": {"routing_schema": "pipewire_dual_stream.v1"},
            }
        )


def test_capture_profile_rejects_invalid_metadata_shape() -> None:
    profile_data = get_capture_profile(CaptureProfileId.ONLINE_DUAL_STREAM).to_dict()
    profile_data["metadata"] = {"routing_schema": 1}

    with pytest.raises(ValueError, match="capture profile metadata"):
        CaptureProfile.from_dict(profile_data)


def test_capture_profile_rejects_modified_hardcoded_definition() -> None:
    profile_data = get_capture_profile(CaptureProfileId.ONLINE_DUAL_STREAM).to_dict()
    profile_data["cli_name"] = "in-person"

    with pytest.raises(ValueError, match="must use CLI alias"):
        CaptureProfile.from_dict(profile_data)


def test_get_capture_profile_rejects_unknown_durable_id() -> None:
    with pytest.raises(ValueError, match="Unknown capture profile 'online'"):
        get_capture_profile("online")


def test_get_capture_profile_rejects_missing_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        profiles_module,
        "_PROFILE_DEFINITIONS_BY_ID",
        {
            key: value
            for key, value in profiles_module._PROFILE_DEFINITIONS_BY_ID.items()
            if key is not CaptureProfileId.ONLINE_DUAL_STREAM
        },
    )

    with pytest.raises(ValueError, match="Unknown capture profile 'online_dual_stream'"):
        get_capture_profile(CaptureProfileId.ONLINE_DUAL_STREAM)
