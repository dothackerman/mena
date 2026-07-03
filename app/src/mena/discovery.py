"""Read-only PipeWire topology discovery for capture planning."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Self

from mena.fields import optional_str, required_bool, required_int, required_list, required_str
from mena.profiles import CaptureProfileId

DISCOVERY_VERSION = 1


class DiscoveryStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class AudioRoute:
    """A discovered PipeWire source or sink."""

    id: int
    route_id: str
    name: str
    description: str | None
    media_class: str
    active: bool
    is_default: bool = False
    node_name: str | None = None
    object_serial: str | None = None
    pw_record_target: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "route_id": self.route_id,
            "name": self.name,
            "description": self.description,
            "media_class": self.media_class,
            "active": self.active,
            "is_default": self.is_default,
            "node_name": self.node_name,
            "object_serial": self.object_serial,
            "pw_record_target": self.pw_record_target,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Self:
        return cls(
            id=_required_int(data, "id"),
            route_id=_required_str(data, "route_id"),
            name=_required_str(data, "name"),
            description=_optional_str(data, "description"),
            media_class=_required_str(data, "media_class"),
            active=_required_bool(data, "active"),
            is_default=_optional_bool(data, "is_default") or False,
            node_name=_optional_str(data, "node_name"),
            object_serial=_optional_str(data, "object_serial"),
            pw_record_target=_optional_str(data, "pw_record_target"),
        )


@dataclass(frozen=True)
class PlaybackStream:
    """A discovered active playback stream that may contain meeting audio."""

    id: int
    route_id: str
    name: str
    app_binary_name: str | None
    stream_name: str | None
    media_name: str | None
    sink_input_id: str | None
    active: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "route_id": self.route_id,
            "name": self.name,
            "app_binary_name": self.app_binary_name,
            "stream_name": self.stream_name,
            "media_name": self.media_name,
            "sink_input_id": self.sink_input_id,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Self:
        return cls(
            id=_required_int(data, "id"),
            route_id=_required_str(data, "route_id"),
            name=_required_str(data, "name"),
            app_binary_name=_optional_str(data, "app_binary_name"),
            stream_name=_optional_str(data, "stream_name"),
            media_name=_optional_str(data, "media_name"),
            sink_input_id=_optional_str(data, "sink_input_id"),
            active=_required_bool(data, "active"),
        )


@dataclass(frozen=True)
class AudioDiscovery:
    """Structured discovery facts persisted by the session orchestrator."""

    version: int
    profile: CaptureProfileId
    status: DiscoveryStatus
    sources: tuple[AudioRoute, ...]
    sinks: tuple[AudioRoute, ...]
    playback_streams: tuple[PlaybackStream, ...]
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "profile": self.profile.value,
            "status": self.status.value,
            "sources": [source.to_dict() for source in self.sources],
            "sinks": [sink.to_dict() for sink in self.sinks],
            "playback_streams": [stream.to_dict() for stream in self.playback_streams],
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Self:
        sources = _required_list(data, "sources")
        sinks = _required_list(data, "sinks")
        streams = _required_list(data, "playback_streams")
        return cls(
            version=_required_int(data, "version"),
            profile=CaptureProfileId(_required_str(data, "profile")),
            status=DiscoveryStatus(_required_str(data, "status")),
            sources=tuple(AudioRoute.from_dict(_required_dict(source)) for source in sources),
            sinks=tuple(AudioRoute.from_dict(_required_dict(sink)) for sink in sinks),
            playback_streams=tuple(
                PlaybackStream.from_dict(_required_dict(stream)) for stream in streams
            ),
            error=_optional_str(data, "error"),
        )


Runner = Callable[..., subprocess.CompletedProcess[str]]


def discover_audio_routes(
    profile: CaptureProfileId,
    *,
    runner: Runner = subprocess.run,
) -> AudioDiscovery:
    """Discover PipeWire routes without mutating the audio graph."""

    try:
        completed = runner(["pw-dump"], capture_output=True, text=True, check=True)
    except FileNotFoundError:
        return _unavailable(profile, "PipeWire command 'pw-dump' was not found.")
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() if exc.stderr else str(exc)
        return _unavailable(profile, f"PipeWire discovery failed: {message}")

    try:
        graph = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return _unavailable(profile, f"PipeWire discovery returned invalid JSON: {exc.msg}")
    if not isinstance(graph, list):
        return _unavailable(profile, "PipeWire discovery returned an unexpected JSON shape.")

    default_source_name, default_sink_name = _discover_default_route_names(runner)
    sources, sinks, playback_streams = _parse_pw_dump(
        graph,
        default_source_name=default_source_name,
        default_sink_name=default_sink_name,
    )
    if profile is CaptureProfileId.IN_PERSON_SINGLE_MIC:
        sinks = ()
        playback_streams = ()

    return AudioDiscovery(
        version=DISCOVERY_VERSION,
        profile=profile,
        status=DiscoveryStatus.AVAILABLE,
        sources=sources,
        sinks=sinks,
        playback_streams=playback_streams,
    )


def format_discovery(discovery: AudioDiscovery) -> str:
    """Return a stable human-readable discovery summary."""

    lines = [f"Audio discovery: {discovery.status.value} ({discovery.profile.value})"]
    if discovery.error:
        lines.append(f"Error: {discovery.error}")
        return "\n".join(lines)

    lines.append("Microphones:")
    lines.extend(_format_routes(discovery.sources))
    if discovery.profile is CaptureProfileId.ONLINE_DUAL_STREAM:
        lines.append("Playback streams:")
        lines.extend(_format_streams(discovery.playback_streams))
        lines.append("Sinks:")
        lines.extend(_format_routes(discovery.sinks))
    return "\n".join(lines)


def _parse_pw_dump(
    graph: Sequence[object],
    *,
    default_source_name: str | None = None,
    default_sink_name: str | None = None,
) -> tuple[
    tuple[AudioRoute, ...],
    tuple[AudioRoute, ...],
    tuple[PlaybackStream, ...],
]:
    sources: list[AudioRoute] = []
    sinks: list[AudioRoute] = []
    playback_streams: list[PlaybackStream] = []

    for item in graph:
        if not isinstance(item, dict):
            continue
        info = item.get("info")
        if not isinstance(info, dict):
            continue
        props = info.get("props")
        if not isinstance(props, dict):
            continue
        media_class = _prop(props, "media.class")
        node_id = item.get("id")
        if not isinstance(node_id, int) or not media_class:
            continue

        if media_class == "Audio/Source/Internal":
            continue

        if media_class.startswith("Audio/Source"):
            sources.append(
                _route_from_props(
                    node_id,
                    media_class,
                    info,
                    props,
                    default_route_name=default_source_name,
                )
            )
        elif media_class.startswith("Audio/Sink"):
            sinks.append(
                _route_from_props(
                    node_id,
                    media_class,
                    info,
                    props,
                    default_route_name=default_sink_name,
                )
            )
        elif media_class == "Stream/Output/Audio":
            playback_streams.append(_stream_from_props(node_id, info, props))

    return (
        tuple(sorted(sources, key=_route_sort_key)),
        tuple(sorted(sinks, key=_route_sort_key)),
        tuple(sorted(playback_streams, key=_stream_sort_key)),
    )


def _route_sort_key(route: AudioRoute) -> tuple[int, str]:
    return (route.id, route.route_id)


def _stream_sort_key(stream: PlaybackStream) -> tuple[int, str]:
    return (stream.id, stream.route_id)


def _route_from_props(
    node_id: int,
    media_class: str,
    info: dict[str, object],
    props: dict[object, object],
    *,
    default_route_name: str | None = None,
) -> AudioRoute:
    node_name = _prop(props, "node.name")
    object_serial = _object_serial(props)
    is_default = node_name is not None and node_name == default_route_name
    return AudioRoute(
        id=node_id,
        route_id=f"pipewire:node:{node_id}",
        name=node_name or f"node-{node_id}",
        description=_prop(props, "node.description"),
        media_class=media_class,
        active=_is_active(info) or is_default,
        is_default=is_default,
        node_name=node_name,
        object_serial=object_serial,
        pw_record_target=node_name or object_serial,
    )


def _stream_from_props(
    node_id: int,
    info: dict[str, object],
    props: dict[object, object],
) -> PlaybackStream:
    sink_input_id = _object_serial(props)
    return PlaybackStream(
        id=node_id,
        route_id=(
            f"pulse:sink-input:{sink_input_id}" if sink_input_id else f"pipewire:node:{node_id}"
        ),
        name=_prop(props, "node.name") or f"node-{node_id}",
        app_binary_name=_prop(props, "application.process.binary"),
        stream_name=_prop(props, "stream.name"),
        media_name=_prop(props, "media.name"),
        sink_input_id=sink_input_id,
        active=_is_active(info),
    )


def _is_active(info: dict[str, object]) -> bool:
    state = info.get("state")
    return state in {"running", "active"}


def _discover_default_route_names(runner: Runner) -> tuple[str | None, str | None]:
    completed = _run_pw_metadata(runner)
    if completed is None:
        return (None, None)

    default_source_name = _default_route_name(completed.stdout, "default.audio.source")
    default_sink_name = _default_route_name(completed.stdout, "default.audio.sink")
    return (default_source_name, default_sink_name)


def _run_pw_metadata(runner: Runner) -> subprocess.CompletedProcess[str] | None:
    try:
        return runner(["pw-metadata", "-n", "default"], capture_output=True, text=True, check=True)
    except FileNotFoundError:
        return None
    except subprocess.CalledProcessError:
        try:
            return runner(["pw-metadata"], capture_output=True, text=True, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError):
            return None


def _default_route_name(metadata_output: str, key: str) -> str | None:
    key_token = f"key:'{key}'"
    for line in metadata_output.splitlines():
        if key_token not in line:
            continue
        _, _, value_part = line.partition(" value:'")
        if not value_part:
            continue
        raw_value, _, _ = value_part.partition("'")
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        name = value.get("name")
        if isinstance(name, str) and name:
            return name
    return None


def _prop(props: dict[object, object], key: str) -> str | None:
    value = props.get(key)
    return value if isinstance(value, str) and value else None


def _object_serial(props: dict[object, object]) -> str | None:
    value = props.get("object.serial")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.isdigit():
        return value
    return None


def _unavailable(profile: CaptureProfileId, error: str) -> AudioDiscovery:
    return AudioDiscovery(
        version=DISCOVERY_VERSION,
        profile=profile,
        status=DiscoveryStatus.UNAVAILABLE,
        sources=(),
        sinks=(),
        playback_streams=(),
        error=error,
    )


def _format_routes(routes: tuple[AudioRoute, ...]) -> list[str]:
    if not routes:
        return ["  - none"]
    return [
        f"  - {route.route_id} {route.name}"
        f"{f' ({route.description})' if route.description else ''}"
        f" [{_activity_label(route.active)}]"
        for route in routes
    ]


def _format_streams(streams: tuple[PlaybackStream, ...]) -> list[str]:
    if not streams:
        return ["  - none"]
    lines = []
    for stream in streams:
        details = ", ".join(
            detail
            for detail in (stream.app_binary_name, stream.stream_name, stream.media_name)
            if detail
        )
        lines.append(
            f"  - {stream.route_id} {stream.name}"
            f"{f' ({details})' if details else ''}"
            f" [{_activity_label(stream.active)}]"
        )
    return lines


def _activity_label(active: bool) -> str:
    return "active" if active else "idle"


def _required_int(data: dict[str, object], key: str) -> int:
    return required_int(
        data,
        key,
        error=ValueError,
        message=f"discovery.{key} must be an integer.",
    )


def _required_bool(data: dict[str, object], key: str) -> bool:
    return required_bool(
        data,
        key,
        error=ValueError,
        message=f"discovery.{key} must be a boolean.",
    )


def _optional_bool(data: dict[str, object], key: str) -> bool | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"discovery.{key} must be a boolean or null.")
    return value


def _required_str(data: dict[str, object], key: str) -> str:
    return required_str(
        data,
        key,
        error=ValueError,
        message=f"discovery.{key} must be a non-empty string.",
    )


def _optional_str(data: dict[str, object], key: str) -> str | None:
    return optional_str(
        data,
        key,
        error=ValueError,
        message=f"discovery.{key} must be a non-empty string or null.",
    )


def _required_list(data: dict[str, object], key: str) -> list[object]:
    return required_list(
        data,
        key,
        error=ValueError,
        message=f"discovery.{key} must be a list.",
    )


def _required_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("discovery item must be an object.")
    return value
