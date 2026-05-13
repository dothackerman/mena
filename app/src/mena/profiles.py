"""Capture profile contract supported by the PoC CLI and capture worker."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Self, TypeVar

EnumValue = TypeVar("EnumValue", bound=StrEnum)


class CaptureProfileId(StrEnum):
    """Durable architecture-facing capture profile identifiers."""

    ONLINE_DUAL_STREAM = "online_dual_stream"
    IN_PERSON_SINGLE_MIC = "in_person_single_mic"


class CaptureProfileCliName(StrEnum):
    """User-facing capture profile names for the first PoC CLI slice."""

    ONLINE = "online"
    IN_PERSON = "in-person"


class CaptureSourceId(StrEnum):
    """Durable logical source identifiers persisted in session metadata."""

    LOCAL_MIC = "local_mic"
    SYSTEM_AUDIO = "system_audio"
    ROOM_MIC = "room_mic"


class CaptureSemanticRole(StrEnum):
    """Meaning of a capture source for downstream processing."""

    LOCAL_MIC = "local_mic"
    SYSTEM_AUDIO = "system_audio"
    ROOM_MIC = "room_mic"
    SYSTEM_MIX_REMOTE = "system_mix_remote"


class ChannelLayout(StrEnum):
    """Channel layout expected from a capture source."""

    MONO = "mono"
    STEREO = "stereo"


class DiarisationPolicy(StrEnum):
    """Diarisation expectation for a capture source."""

    NONE = "none"
    WHOLE_STREAM = "whole_stream"
    PER_CHANNEL = "per_channel"


@dataclass(frozen=True)
class DeviceRoute:
    """Concrete operating-system route selected for a logical capture source."""

    backend: str
    route_id: str
    label: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "route_id": self.route_id,
            "label": self.label,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Self:
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in metadata.items()
        ):
            raise ValueError("device_route.metadata must be an object with string keys and values.")
        return cls(
            backend=_required_str(data, "backend", "device_route"),
            route_id=_required_str(data, "route_id", "device_route"),
            label=_optional_str(data, "label", "device_route"),
            metadata=dict(metadata),
        )


@dataclass(frozen=True)
class CaptureSource:
    """Logical source required by a capture profile."""

    logical_id: CaptureSourceId
    semantic_role: CaptureSemanticRole
    channel_layout: ChannelLayout
    diarisation_policy: DiarisationPolicy
    device_route: DeviceRoute | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "logical_id": self.logical_id.value,
            "semantic_role": self.semantic_role.value,
            "channel_layout": self.channel_layout.value,
            "diarisation_policy": self.diarisation_policy.value,
            "device_route": self.device_route.to_dict() if self.device_route else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Self:
        route_data = data.get("device_route")
        if route_data is not None and not isinstance(route_data, dict):
            raise ValueError("capture source device_route must be an object or null.")
        return cls(
            logical_id=_enum_value(CaptureSourceId, data, "logical_id", "capture source"),
            semantic_role=_enum_value(CaptureSemanticRole, data, "semantic_role", "capture source"),
            channel_layout=_enum_value(ChannelLayout, data, "channel_layout", "capture source"),
            diarisation_policy=_enum_value(
                DiarisationPolicy,
                data,
                "diarisation_policy",
                "capture source",
            ),
            device_route=DeviceRoute.from_dict(route_data) if route_data is not None else None,
        )


@dataclass(frozen=True)
class CaptureProfile:
    """Durable capture profile definition."""

    name: CaptureProfileId
    cli_name: CaptureProfileCliName
    sources: tuple[CaptureSource, ...]
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name.value,
            "cli_name": self.cli_name.value,
            "sources": [source.to_dict() for source in self.sources],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Self:
        sources = data.get("sources")
        if not isinstance(sources, list):
            raise ValueError("capture profile sources must be a list.")
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in metadata.items()
        ):
            raise ValueError(
                "capture profile metadata must be an object with string keys and values."
            )

        parsed_sources = []
        for source in sources:
            _ensure_dict(source, "capture profile source")
            parsed_sources.append(CaptureSource.from_dict(source))

        profile = cls(
            name=_enum_value(CaptureProfileId, data, "name", "capture profile"),
            cli_name=_enum_value(CaptureProfileCliName, data, "cli_name", "capture profile"),
            sources=tuple(parsed_sources),
            metadata=dict(metadata),
        )
        _validate_profile(profile)
        return profile


@dataclass(frozen=True)
class CaptureProfileSelection:
    """Capture profile selected through the CLI."""

    id: CaptureProfileId
    cli_name: CaptureProfileCliName


_PROFILE_SELECTIONS_BY_CLI_NAME = {
    CaptureProfileCliName.ONLINE.value: CaptureProfileSelection(
        id=CaptureProfileId.ONLINE_DUAL_STREAM,
        cli_name=CaptureProfileCliName.ONLINE,
    ),
    CaptureProfileCliName.IN_PERSON.value: CaptureProfileSelection(
        id=CaptureProfileId.IN_PERSON_SINGLE_MIC,
        cli_name=CaptureProfileCliName.IN_PERSON,
    ),
}

_PROFILE_DEFINITIONS_BY_ID = {
    CaptureProfileId.ONLINE_DUAL_STREAM: CaptureProfile(
        name=CaptureProfileId.ONLINE_DUAL_STREAM,
        cli_name=CaptureProfileCliName.ONLINE,
        sources=(
            CaptureSource(
                logical_id=CaptureSourceId.LOCAL_MIC,
                semantic_role=CaptureSemanticRole.LOCAL_MIC,
                channel_layout=ChannelLayout.MONO,
                diarisation_policy=DiarisationPolicy.NONE,
            ),
            CaptureSource(
                logical_id=CaptureSourceId.SYSTEM_AUDIO,
                semantic_role=CaptureSemanticRole.SYSTEM_AUDIO,
                channel_layout=ChannelLayout.MONO,
                diarisation_policy=DiarisationPolicy.WHOLE_STREAM,
            ),
        ),
        metadata={"routing_schema": "pipewire_dual_stream.v1"},
    ),
    CaptureProfileId.IN_PERSON_SINGLE_MIC: CaptureProfile(
        name=CaptureProfileId.IN_PERSON_SINGLE_MIC,
        cli_name=CaptureProfileCliName.IN_PERSON,
        sources=(
            CaptureSource(
                logical_id=CaptureSourceId.ROOM_MIC,
                semantic_role=CaptureSemanticRole.ROOM_MIC,
                channel_layout=ChannelLayout.STEREO,
                diarisation_policy=DiarisationPolicy.WHOLE_STREAM,
            ),
        ),
        metadata={"routing_schema": "pipewire_single_mic.v1"},
    ),
}


def parse_capture_profile(value: str) -> CaptureProfileSelection:
    """Parse a CLI profile value into a durable architecture profile selection."""

    try:
        return _PROFILE_SELECTIONS_BY_CLI_NAME[value]
    except KeyError as exc:
        valid_profiles = ", ".join(capture_profile_choices())
        message = f"Unknown capture profile '{value}'. Valid profiles: {valid_profiles}."
        raise ValueError(message) from exc


def capture_profile_choices() -> tuple[str, ...]:
    """Return profile values in the order they should appear in CLI help."""

    return tuple(profile.value for profile in CaptureProfileCliName)


def get_capture_profile(profile_id: CaptureProfileId | str) -> CaptureProfile:
    """Return a hardcoded profile definition by durable identifier."""

    try:
        durable_id = CaptureProfileId(profile_id)
    except ValueError as exc:
        valid_profiles = ", ".join(profile.value for profile in CaptureProfileId)
        raise ValueError(
            f"Unknown capture profile '{profile_id}'. Valid profiles: {valid_profiles}."
        ) from exc
    try:
        return _PROFILE_DEFINITIONS_BY_ID[durable_id]
    except KeyError as exc:
        valid_profiles = ", ".join(profile.value for profile in CaptureProfileId)
        raise ValueError(
            f"Unknown capture profile '{profile_id}'. Valid profiles: {valid_profiles}."
        ) from exc


def profile_for_selection(selection: CaptureProfileSelection) -> CaptureProfile:
    """Return the durable profile definition selected by the CLI."""

    return get_capture_profile(selection.id)


def _validate_profile(profile: CaptureProfile) -> None:
    expected = _PROFILE_DEFINITIONS_BY_ID.get(profile.name)
    if expected is None:
        valid_profiles = ", ".join(profile.value for profile in CaptureProfileId)
        raise ValueError(
            f"Unknown capture profile '{profile.name}'. Valid profiles: {valid_profiles}."
        )
    if profile.cli_name != expected.cli_name:
        raise ValueError(
            f"Capture profile '{profile.name}' must use CLI alias '{expected.cli_name}'."
        )
    if tuple(_source_contract(source) for source in profile.sources) != tuple(
        _source_contract(source) for source in expected.sources
    ):
        raise ValueError(f"Capture profile '{profile.name}' has an invalid source definition.")


def _source_contract(
    source: CaptureSource,
) -> tuple[CaptureSourceId, CaptureSemanticRole, ChannelLayout, DiarisationPolicy]:
    return (
        source.logical_id,
        source.semantic_role,
        source.channel_layout,
        source.diarisation_policy,
    )


def _required_str(data: dict[str, object], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}.{key} must be a non-empty string.")
    return value


def _optional_str(data: dict[str, object], key: str, label: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}.{key} must be a non-empty string or null.")
    return value


def _enum_value(
    enum_type: type[EnumValue],
    data: dict[str, object],
    key: str,
    label: str,
) -> EnumValue:
    raw_value = _required_str(data, key, label)
    try:
        return enum_type(raw_value)
    except ValueError as exc:
        valid_values = ", ".join(value.value for value in enum_type)
        raise ValueError(
            f"Unknown {label}.{key} '{raw_value}'. Valid values: {valid_values}."
        ) from exc


def _ensure_dict(value: object, label: str) -> bool:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object.")
    return True
