"""Capture route planning for the record command."""

from __future__ import annotations

from dataclasses import dataclass

from mena.capture import DEFAULT_SEGMENT_SECONDS
from mena.discovery import AudioDiscovery, AudioRoute, PlaybackStream
from mena.profiles import CaptureProfileId
from mena.protocol import CaptureRouteBindingPayload, StartCapturePayload


class CapturePlanningError(ValueError):
    """Raised when selected routes do not satisfy the capture profile."""


@dataclass(frozen=True)
class RouteSelections:
    """Concrete route ids selected before capture starts."""

    microphone_route_id: str
    playback_stream_route_id: str | None = None
    output_sink_route_id: str | None = None


def build_start_capture_payload(
    *,
    profile: CaptureProfileId,
    language: str,
    discovery: AudioDiscovery,
    selections: RouteSelections,
    segment_seconds: int = DEFAULT_SEGMENT_SECONDS,
) -> StartCapturePayload:
    """Validate route selections and produce the worker start-capture payload."""

    if discovery.profile is not profile:
        raise CapturePlanningError(
            f"Audio discovery profile '{discovery.profile.value}' does not match "
            f"selected profile '{profile.value}'."
        )
    if segment_seconds < 1:
        raise CapturePlanningError("Segment seconds must be positive.")

    microphone = _find_audio_route(
        discovery.sources,
        selections.microphone_route_id,
        "microphone",
    )
    _require_route_prefix(microphone.route_id, "pipewire:node:", "microphone")
    routes: list[CaptureRouteBindingPayload] = []
    selected_sources: tuple[str, ...]

    if profile is CaptureProfileId.IN_PERSON_SINGLE_MIC:
        if (
            selections.playback_stream_route_id is not None
            or selections.output_sink_route_id is not None
        ):
            raise CapturePlanningError("In-person capture accepts a microphone route only.")
        routes.append(
            _route_binding(
                role="microphone",
                source_id="room_mic",
                route_id=microphone.route_id,
                label=_audio_route_label(microphone),
                backend_name=microphone.name,
                backend_target=_required_pw_record_target(microphone, "microphone"),
                node_name=microphone.node_name,
                object_serial=microphone.object_serial,
            )
        )
        selected_sources = ("room_mic",)
    else:
        if selections.playback_stream_route_id is None:
            raise CapturePlanningError("Online capture requires a playback stream route.")
        if selections.output_sink_route_id is None:
            raise CapturePlanningError("Online capture requires an output sink route.")
        playback_stream = _find_playback_stream(
            discovery.playback_streams,
            selections.playback_stream_route_id,
        )
        _require_route_prefix(
            playback_stream.route_id,
            "pulse:sink-input:",
            "playback stream",
        )
        output_sink = _find_audio_route(
            discovery.sinks,
            selections.output_sink_route_id,
            "output sink",
        )
        _require_route_prefix(output_sink.route_id, "pipewire:node:", "output sink")
        routes.extend(
            (
                _route_binding(
                    role="microphone",
                    source_id="local_mic",
                    route_id=microphone.route_id,
                    label=_audio_route_label(microphone),
                    backend_name=microphone.name,
                    backend_target=_required_pw_record_target(microphone, "microphone"),
                    node_name=microphone.node_name,
                    object_serial=microphone.object_serial,
                ),
                _route_binding(
                    role="playback_stream",
                    source_id="system_audio",
                    route_id=playback_stream.route_id,
                    label=_playback_stream_label(playback_stream),
                    backend_name=playback_stream.name,
                    backend_target=_required_sink_input_id(playback_stream),
                    sink_input_id=playback_stream.sink_input_id,
                ),
                _route_binding(
                    role="output_sink",
                    source_id=None,
                    route_id=output_sink.route_id,
                    label=_audio_route_label(output_sink),
                    backend_name=output_sink.name,
                    backend_target=_required_pw_record_target(output_sink, "output sink"),
                    node_name=output_sink.node_name,
                    object_serial=output_sink.object_serial,
                ),
            )
        )
        selected_sources = ("local_mic", "system_audio")

    return StartCapturePayload(
        profile=profile.value,
        language=language,
        segment_seconds=segment_seconds,
        selected_sources=selected_sources,
        routes=tuple(routes),
    )


def _find_audio_route(
    routes: tuple[AudioRoute, ...],
    route_id: str,
    role: str,
) -> AudioRoute:
    for route in routes:
        if route.route_id == route_id:
            return route
    raise CapturePlanningError(f"Selected {role} route '{route_id}' was not discovered.")


def _find_playback_stream(
    streams: tuple[PlaybackStream, ...],
    route_id: str,
) -> PlaybackStream:
    for stream in streams:
        if stream.route_id == route_id:
            return stream
    raise CapturePlanningError(f"Selected playback stream route '{route_id}' was not discovered.")


def _route_binding(
    *,
    role: str,
    source_id: str | None,
    route_id: str,
    label: str,
    backend_name: str | None,
    backend_target: str | None = None,
    node_name: str | None = None,
    object_serial: str | None = None,
    sink_input_id: str | None = None,
) -> CaptureRouteBindingPayload:
    return CaptureRouteBindingPayload(
        role=role,
        source_id=source_id,
        route_id=route_id,
        label=label,
        backend=_route_backend(route_id),
        backend_name=backend_name,
        backend_target=backend_target,
        node_name=node_name,
        object_serial=object_serial,
        sink_input_id=sink_input_id,
        required=True,
    )


def _required_pw_record_target(route: AudioRoute, role: str) -> str:
    if route.pw_record_target:
        return route.pw_record_target
    raise CapturePlanningError(f"Selected {role} route is missing a PipeWire record target.")


def _required_sink_input_id(stream: PlaybackStream) -> str:
    if stream.sink_input_id:
        return stream.sink_input_id
    raise CapturePlanningError("Selected playback stream route is missing a Pulse sink-input id.")


def _audio_route_label(route: AudioRoute) -> str:
    return route.description or route.name


def _playback_stream_label(stream: PlaybackStream) -> str:
    parts = [stream.media_name or stream.stream_name or stream.name]
    if stream.app_binary_name:
        parts.append(stream.app_binary_name)
    return " - ".join(parts)


def _route_backend(route_id: str) -> str:
    if ":" not in route_id:
        return "unknown"
    return route_id.split(":", 1)[0]


def _require_route_prefix(route_id: str, prefix: str, role: str) -> None:
    if not route_id.startswith(prefix):
        raise CapturePlanningError(
            f"Selected {role} route '{route_id}' is not supported by the PipeWire "
            f"capture adapter; expected '{prefix}…'."
        )
