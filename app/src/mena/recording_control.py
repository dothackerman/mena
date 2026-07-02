"""Recording control workflow orchestration."""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import wave
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from mena.capture_planning import (
    CapturePlanningError,
    RouteSelections,
    build_start_capture_payload,
)
from mena.discovery import (
    AudioDiscovery,
    AudioRoute,
    DiscoveryStatus,
    PlaybackStream,
)
from mena.interaction import (
    ChoiceOption,
    ChoiceRequest,
    InteractionPort,
    TextRequest,
    WorkflowNotice,
)
from mena.profiles import CaptureProfileId, CaptureSourceId
from mena.protocol import (
    CaptureFinalizedPayload,
    CaptureRouteBindingPayload,
    CaptureRouteEventPayload,
    CaptureStatusPayload,
    ErrorPayload,
    ProtocolError,
    ProtocolMessage,
    SegmentFlushedPayload,
    SegmentsFlushedPayload,
    StartCapturePayload,
    WorkerExitedError,
    WorkerProcess,
)
from mena.sessions import (
    append_session_degradation,
    append_session_failure,
    create_session,
    update_session_capture_metadata,
    update_session_capture_plan,
    update_session_discovery,
    update_session_record,
)
from mena.timestamps import utc_timestamp

WORKER_EXIT_TIMEOUT_SECONDS = 30.0
ChoiceContext = dict[str, object]


@dataclass(frozen=True)
class RecordCommandInput:
    profile: Any
    language: str
    name: str | None = None
    microphone_route_id: str | None = None
    playback_stream_route_id: str | None = None
    output_sink_route_id: str | None = None
    dry_run: bool = False
    verbose: bool = False
    worker_exit_timeout: float = WORKER_EXIT_TIMEOUT_SECONDS
    stop_input_available: bool = True


@dataclass(frozen=True)
class RecordingControlResult:
    exit_code: int
    record_path: Path | None = None
    final_status: str | None = None


class _CaptureEventMonitor:
    """Drain worker capture events while the foreground prompt waits for stop."""

    def __init__(
        self,
        worker: WorkerProcess,
        *,
        record_path: Path,
        start_payload: StartCapturePayload,
        interaction: InteractionPort,
    ) -> None:
        self.worker = worker
        self.record_path = record_path
        self.start_payload = start_payload
        self.interaction = interaction
        self.segments: list[dict[str, object]] = []
        self._terminal_events: queue.Queue[ProtocolMessage | BaseException] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run,
            name="mena-capture-events",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def wait_for_finalized(self) -> ProtocolMessage:
        event = self._terminal_events.get()
        self._thread.join()
        if isinstance(event, BaseException):
            raise event
        return event

    def poll_terminal_event(self) -> ProtocolMessage | None:
        try:
            event = self._terminal_events.get_nowait()
        except queue.Empty:
            return None
        self._thread.join()
        if isinstance(event, BaseException):
            raise event
        return event

    def _run(self) -> None:
        while True:
            try:
                message = self.worker.receive()
                should_continue = self._handle_message(message)
            except BaseException as exc:
                self._terminal_events.put(exc)
                return
            if not should_continue:
                return

    def _handle_message(self, message: ProtocolMessage) -> bool:
        if message.type == "segment_flushed":
            segment = SegmentFlushedPayload.from_dict(message.payload)
            self._persist_segments((dict(message.payload),))
            self._print_segment(segment)
            return True
        if message.type == "segments_flushed":
            batch = SegmentsFlushedPayload.from_dict(message.payload)
            raw_segments = message.payload["segments"]
            if not isinstance(raw_segments, list):
                raise ProtocolError("segments_flushed payload segments must be a list.")
            self._persist_segments(
                tuple(cast(dict[str, object], segment) for segment in raw_segments)
            )
            for segment in batch.segments:
                self._print_segment(segment)
            return True
        if message.type == "capture_status":
            status = CaptureStatusPayload.from_dict(message.payload)
            self.interaction.notify(
                WorkflowNotice(
                    topic="record.capture_status",
                    context={
                        "elapsed_seconds": status.elapsed_seconds,
                        "sealed_segment_count": status.sealed_segment_count,
                    },
                )
            )
            return True
        if message.type == "capture_route_event":
            event = CaptureRouteEventPayload.from_dict(message.payload)
            append_session_degradation(self.record_path, event.to_dict())
            self.interaction.notify(
                WorkflowNotice(
                    topic="record.capture_route_event",
                    severity="warning" if event.event != "recovered" else "info",
                    context={"event": event.event, "message": event.message},
                )
            )
            return True
        if message.type == "capture_finalized":
            self._terminal_events.put(message)
            return False
        if message.type == "error":
            error = ErrorPayload.from_dict(message.payload)
            self._terminal_events.put(ProtocolError(error.message))
            return False
        self._terminal_events.put(ProtocolError(f"Unexpected capture event '{message.type}'."))
        return False

    def _persist_segments(self, segments: tuple[dict[str, object], ...]) -> None:
        self.segments.extend(segments)
        # Incremental metadata is intentional: a session can be interrupted
        # before finalization and still retain processable sealed chunks.
        update_session_capture_metadata(
            self.record_path,
            _capture_metadata_from_segments(
                self.start_payload,
                self.segments,
            ),
        )

    def _print_segment(self, segment: SegmentFlushedPayload) -> None:
        self.interaction.notify(
            WorkflowNotice(
                topic="record.segment_flushed",
                context={"source_id": segment.source_id, "index": segment.index},
            )
        )


def run_recording_control_workflow(
    command: RecordCommandInput,
    *,
    data_root: Path | None,
    discoverer: Callable[[CaptureProfileId], AudioDiscovery],
    worker_factory: Callable[..., AbstractContextManager[Any]],
    interaction: InteractionPort,
    now: datetime | None,
) -> RecordingControlResult:
    created_session = create_session(
        profile=command.profile,
        language=command.language,
        name=command.name,
        data_root=data_root,
    )
    interaction.notify(
        WorkflowNotice(
            topic="record.session_created",
            context={"session_id": created_session.record.id},
        )
    )
    interaction.notify(
        WorkflowNotice(
            topic="record.session_record",
            context={"record_path": created_session.record_path},
        )
    )
    discovery = discoverer(command.profile.id)
    update_session_discovery(created_session.record_path, discovery.to_dict())
    interaction.notify(WorkflowNotice(topic="record.discovery", context={"discovery": discovery}))
    if command.dry_run:
        return RecordingControlResult(exit_code=0, record_path=created_session.record_path)
    if discovery.status is not DiscoveryStatus.AVAILABLE:
        interaction.notify(WorkflowNotice(topic="record.capture_not_started.discovery_unavailable"))
        return RecordingControlResult(exit_code=1, record_path=created_session.record_path)
    try:
        route_selections = _resolve_route_selections(
            profile=command.profile.id,
            discovery=discovery,
            microphone_route_id=command.microphone_route_id,
            playback_stream_route_id=command.playback_stream_route_id,
            output_sink_route_id=command.output_sink_route_id,
            interaction=interaction,
        )
        start_payload = build_start_capture_payload(
            profile=command.profile.id,
            language=command.language,
            discovery=discovery,
            selections=route_selections,
        )
        if command.verbose:
            interaction.notify(
                WorkflowNotice(
                    topic="record.diagnostics",
                    context={"start_payload": start_payload},
                )
            )
    except (EOFError, KeyboardInterrupt):
        interaction.notify(WorkflowNotice(topic="record.setup_cancelled"))
        return RecordingControlResult(exit_code=1, record_path=created_session.record_path)
    except (CapturePlanningError, ValueError) as exc:
        interaction.notify(
            WorkflowNotice(
                topic="record.capture_not_started.error",
                context={"error": str(exc)},
            )
        )
        return RecordingControlResult(exit_code=1, record_path=created_session.record_path)
    if not command.stop_input_available:
        interaction.notify(WorkflowNotice(topic="record.capture_not_started.noninteractive_stop"))
        return RecordingControlResult(exit_code=1, record_path=created_session.record_path)
    update_session_capture_plan(
        created_session.record_path,
        _capture_plan_from_start_payload(start_payload, status="planned"),
    )
    started_at = _utc_timestamp(now)
    update_session_record(
        created_session.record_path,
        updates={
            "status": "recording",
            "recording": _recording_lifecycle(
                started_at=started_at,
                stopped_at=None,
                stop_reason=None,
                finalization_status="pending",
                completed_at=None,
                error=None,
            ),
            "capture_plan": _capture_plan_from_start_payload(
                start_payload,
                status="active",
            ),
        },
    )
    try:
        worker_kwargs: dict[str, object] = {
            "session_id": created_session.record.id,
            "session_dir": created_session.bundle_path,
        }
        if command.verbose:
            worker_kwargs["capture_diagnostics"] = True
        worker_context = worker_factory(**worker_kwargs)
        worker = worker_context.__enter__()
    except (OSError, subprocess.SubprocessError) as exc:
        _persist_capture_failure(
            record_path=created_session.record_path,
            start_payload=start_payload,
            started_at=started_at,
            error_message=str(exc),
            now=now,
        )
        interaction.notify(
            WorkflowNotice(topic="record.capture_failed", context={"error": str(exc)})
        )
        return RecordingControlResult(exit_code=1, record_path=created_session.record_path)
    try:
        capture_monitor = _capture_event_monitor(
            worker,
            record_path=created_session.record_path,
            start_payload=start_payload,
            interaction=interaction,
        )
        interrupted_stop = False
        start_command_sent = False
        monitor_started = False
        finalized: ProtocolMessage | None = None
        try:
            worker.handshake()
            interaction.notify(WorkflowNotice(topic="record.consent_notice"))
            start_command_sent = True
            worker.start_capture(start_payload)
            if capture_monitor is not None:
                capture_monitor.start()
                monitor_started = True
            _wait_for_record_stop(interaction, capture_monitor=capture_monitor)
            finalized = _finalize_recording(
                worker,
                capture_monitor if monitor_started else None,
            )
            exit_code = worker.wait(timeout=command.worker_exit_timeout)
            if exit_code != 0:
                raise WorkerExitedError(f"Worker exited with status {exit_code}.")
        except (EOFError, KeyboardInterrupt):
            if finalized is not None:
                interaction.notify(WorkflowNotice(topic="record.worker_teardown_interrupted"))
            elif not start_command_sent:
                _persist_capture_failure(
                    record_path=created_session.record_path,
                    start_payload=start_payload,
                    started_at=started_at,
                    error_message="Recording setup cancelled.",
                    now=now,
                )
                interaction.notify(WorkflowNotice(topic="record.setup_cancelled"))
                return RecordingControlResult(exit_code=1, record_path=created_session.record_path)
            else:
                interaction.notify(WorkflowNotice(topic="record.stop_interrupted"))
                interrupted_stop = True
                try:
                    finalized = _finalize_recording(
                        worker,
                        capture_monitor if monitor_started else None,
                    )
                    exit_code = worker.wait(timeout=command.worker_exit_timeout)
                    if exit_code != 0:
                        raise WorkerExitedError(f"Worker exited with status {exit_code}.")
                except (
                    ProtocolError,
                    WorkerExitedError,
                    subprocess.TimeoutExpired,
                    ValueError,
                ) as exc:
                    _persist_capture_failure(
                        record_path=created_session.record_path,
                        start_payload=start_payload,
                        started_at=started_at,
                        error_message=str(exc),
                        now=now,
                    )
                    interaction.notify(
                        WorkflowNotice(topic="record.capture_failed", context={"error": str(exc)})
                    )
                    return RecordingControlResult(
                        exit_code=1,
                        record_path=created_session.record_path,
                    )
        except (
            ProtocolError,
            WorkerExitedError,
            subprocess.TimeoutExpired,
            ValueError,
        ) as exc:
            _persist_capture_failure(
                record_path=created_session.record_path,
                start_payload=start_payload,
                started_at=started_at,
                error_message=str(exc),
                now=now,
            )
            interaction.notify(
                WorkflowNotice(topic="record.capture_failed", context={"error": str(exc)})
            )
            return RecordingControlResult(exit_code=1, record_path=created_session.record_path)
    finally:
        worker_context.__exit__(*sys.exc_info())
    finalized_payload = CaptureFinalizedPayload.from_dict(finalized.payload)
    completed_at = finalized_payload.completed_at or _utc_timestamp(now)
    stop_reason = "interrupted" if interrupted_stop else finalized_payload.stop_reason
    capture_metadata = (
        _capture_metadata_from_segments(start_payload, capture_monitor.segments)
        if capture_monitor is not None
        else finalized_payload.capture_metadata
    )
    source_independence_error = _source_independence_error(
        capture_metadata,
        session_dir=created_session.bundle_path,
    )
    final_status = finalized_payload.status
    final_error = finalized_payload.error
    if source_independence_error is not None:
        final_status = "failed"
        final_error = source_independence_error
    update_session_capture_metadata(created_session.record_path, capture_metadata)
    update_session_record(
        created_session.record_path,
        updates={
            "status": final_status,
            "recording": _recording_lifecycle(
                started_at=started_at,
                stopped_at=completed_at,
                stop_reason=stop_reason,
                finalization_status=("finalized" if final_status == "recorded" else final_status),
                completed_at=completed_at,
                error=final_error,
            ),
            "capture_plan": _capture_plan_from_start_payload(
                start_payload,
                status=("finalized" if final_status == "recorded" else final_status),
            ),
        },
    )
    interaction.notify(WorkflowNotice(topic="record.capture_finalized"))
    if final_status == "recorded":
        interaction.notify(
            WorkflowNotice(
                topic="record.process_handoff",
                context={"record_path": created_session.record_path},
            )
        )
    elif final_error:
        interaction.notify(
            WorkflowNotice(
                topic="record.finalized_status_error",
                context={"status": final_status, "error": final_error},
            )
        )
    else:
        interaction.notify(
            WorkflowNotice(
                topic="record.finalized_status",
                context={"status": final_status},
            )
        )
    return RecordingControlResult(
        exit_code=1 if final_status == "failed" else 0,
        record_path=created_session.record_path,
        final_status=final_status,
    )


def _capture_plan_from_start_payload(
    payload: StartCapturePayload,
    *,
    status: str,
) -> dict[str, object]:
    plan = payload.to_dict()
    plan["version"] = 1
    plan["status"] = status
    return plan


def _format_record_diagnostics(payload: StartCapturePayload) -> str:
    lines = [
        "Record diagnostics:",
        f"  profile: {payload.profile}",
        f"  language: {payload.language}",
        f"  segment_seconds: {payload.segment_seconds}",
        f"  selected_sources: {', '.join(payload.selected_sources)}",
        "  routes:",
    ]
    for route in payload.routes:
        lines.append(
            "    "
            f"role={route.role} source={route.source_id or '-'} "
            f"route_id={route.route_id} backend={route.backend} "
            f"backend_name={route.backend_name or '-'} label={route.label}"
        )
    return "\n".join(lines)


def _capture_metadata_from_segments(
    payload: StartCapturePayload,
    segments: list[dict[str, object]],
) -> dict[str, object]:
    sample_rate = _latest_segment_int(segments, "sample_rate", default=48_000)
    channels = _latest_segment_int(segments, "channels", default=1)
    return {
        "version": 1,
        "profile": payload.profile,
        "sample_rate": sample_rate,
        "channels": channels,
        "segment_seconds": payload.segment_seconds,
        "segment_count": len(segments),
        "segments": list(segments),
        "sources": _capture_source_summaries(payload, segments),
    }


def _capture_source_summaries(
    payload: StartCapturePayload,
    segments: list[dict[str, object]],
) -> list[dict[str, object]]:
    routes_by_source = {
        route.source_id: route for route in payload.routes if route.source_id is not None
    }
    ordered_sources = tuple(
        dict.fromkeys(str(segment["source_id"]) for segment in segments if "source_id" in segment)
    )
    summaries: list[dict[str, object]] = []
    for source_id in ordered_sources:
        source_segments = [segment for segment in segments if segment.get("source_id") == source_id]
        route = routes_by_source.get(source_id)
        channel_layout = source_segments[-1].get("channel_layout", "unknown")
        latest_route_id = source_segments[-1].get("route_id")
        latest_route_label = source_segments[-1].get("route_label")
        latest_capture_mode = source_segments[-1].get("capture_mode")
        summaries.append(
            {
                "source_id": source_id,
                "route_id": latest_route_id or (None if route is None else route.route_id),
                "route_label": latest_route_label or (None if route is None else route.label),
                "capture_mode": latest_capture_mode or _capture_mode_for_route(route),
                "channel_layout": channel_layout,
                "segment_count": len(source_segments),
            }
        )
    return summaries


def _source_independence_error(
    capture_metadata: dict[str, object],
    *,
    session_dir: Path | None = None,
) -> str | None:
    if capture_metadata.get("profile") != CaptureProfileId.ONLINE_DUAL_STREAM.value:
        return None
    route_ids = _capture_route_ids_by_source(capture_metadata)
    local_route_id = route_ids.get(CaptureSourceId.LOCAL_MIC.value)
    system_route_id = route_ids.get(CaptureSourceId.SYSTEM_AUDIO.value)
    if local_route_id is None or system_route_id is None or local_route_id == system_route_id:
        return None
    segments = capture_metadata.get("segments")
    if not isinstance(segments, list):
        return None
    local_segments = _sealed_segments_by_index(segments, CaptureSourceId.LOCAL_MIC.value)
    system_segments = _sealed_segments_by_index(segments, CaptureSourceId.SYSTEM_AUDIO.value)
    local_hashes = _segment_hashes(local_segments)
    system_hashes = _segment_hashes(system_segments)
    common_indexes = sorted(set(local_hashes) & set(system_hashes))
    if not common_indexes:
        return None
    if any(local_hashes[index] != system_hashes[index] for index in common_indexes):
        return None
    if _all_same_index_pairs_are_silent(
        local_segments,
        system_segments,
        common_indexes,
        session_dir=session_dir,
    ):
        return None
    return (
        "Online capture source independence failed: local_mic and system_audio "
        "produced identical same-index audio hashes for distinct selected routes."
    )


def _capture_route_ids_by_source(capture_metadata: dict[str, object]) -> dict[str, str]:
    route_ids: dict[str, str] = {}
    sources = capture_metadata.get("sources")
    if not isinstance(sources, list):
        return route_ids
    for source in sources:
        if not isinstance(source, dict):
            continue
        source_id = source.get("source_id")
        route_id = source.get("route_id")
        if isinstance(source_id, str) and isinstance(route_id, str) and route_id:
            route_ids[source_id] = route_id
    return route_ids


def _sealed_segments_by_index(
    segments: list[object],
    source_id: str,
) -> dict[int, dict[str, object]]:
    indexed: dict[int, dict[str, object]] = {}
    for segment in segments:
        if not isinstance(segment, dict) or segment.get("source_id") != source_id:
            continue
        if segment.get("status") != "sealed":
            continue
        index = segment.get("index")
        if isinstance(index, int):
            indexed[index] = segment
    return indexed


def _segment_hashes(segments: dict[int, dict[str, object]]) -> dict[int, str]:
    hashes: dict[int, str] = {}
    for index, segment in segments.items():
        sha256 = segment.get("sha256")
        if isinstance(sha256, str) and sha256:
            hashes[index] = sha256
    return hashes


def _all_same_index_pairs_are_silent(
    local_segments: dict[int, dict[str, object]],
    system_segments: dict[int, dict[str, object]],
    indexes: Sequence[int],
    *,
    session_dir: Path | None,
) -> bool:
    if session_dir is None:
        return False
    for index in indexes:
        local_silent = _segment_wav_is_silent(local_segments[index], session_dir=session_dir)
        system_silent = _segment_wav_is_silent(system_segments[index], session_dir=session_dir)
        if local_silent is not True or system_silent is not True:
            return False
    return True


def _segment_wav_is_silent(segment: dict[str, object], *, session_dir: Path) -> bool | None:
    raw_path = segment.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    path = Path(raw_path)
    if not path.is_absolute():
        path = session_dir / path
    try:
        with wave.open(str(path), "rb") as wav_file:
            frames = wav_file.readframes(wav_file.getnframes())
    except (EOFError, OSError, wave.Error):
        return None
    if not frames:
        return True
    return all(byte == 0 for byte in frames)


def _capture_mode_for_route(route: CaptureRouteBindingPayload | None) -> str | None:
    if route is None:
        return None
    if route.role == "microphone":
        return "selected_microphone"
    if route.role == "playback_stream":
        return "dedicated_route"
    return None


def _latest_segment_int(
    segments: list[dict[str, object]],
    key: str,
    *,
    default: int,
) -> int:
    for segment in reversed(segments):
        value = segment.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return default


def _recording_lifecycle(
    *,
    started_at: str | None,
    stopped_at: str | None,
    stop_reason: str | None,
    finalization_status: str,
    completed_at: str | None,
    error: str | None,
) -> dict[str, object]:
    return {
        "started_at": started_at,
        "stopped_at": stopped_at,
        "stop_reason": stop_reason,
        "finalization": {
            "status": finalization_status,
            "completed_at": completed_at,
            "error": error,
        },
    }


def _persist_capture_failure(
    *,
    record_path: Path,
    start_payload: StartCapturePayload,
    started_at: str,
    error_message: str,
    now: datetime | None,
) -> None:
    completed_at = _utc_timestamp(now)
    append_session_failure(
        record_path,
        {
            "code": "capture_failed",
            "message": error_message or "Capture failed.",
            "source_id": None,
            "occurred_at": completed_at,
            "recoverable": False,
        },
    )
    update_session_record(
        record_path,
        updates={
            "status": "failed",
            "recording": _recording_lifecycle(
                started_at=started_at,
                stopped_at=completed_at,
                stop_reason="error",
                finalization_status="failed",
                completed_at=completed_at,
                error=error_message or "Capture failed.",
            ),
            "capture_plan": _capture_plan_from_start_payload(
                start_payload,
                status="failed",
            ),
        },
    )


def _capture_event_monitor(
    worker: Any,
    *,
    record_path: Path,
    start_payload: StartCapturePayload,
    interaction: InteractionPort,
) -> _CaptureEventMonitor | None:
    if isinstance(worker, WorkerProcess):
        return _CaptureEventMonitor(
            worker,
            record_path=record_path,
            start_payload=start_payload,
            interaction=interaction,
        )
    return None


def _finalize_recording(
    worker: Any,
    capture_monitor: _CaptureEventMonitor | None,
) -> ProtocolMessage:
    if capture_monitor is not None:
        worker.request_stop_capture()
        return capture_monitor.wait_for_finalized()
    finalized = worker.stop_capture()
    if not isinstance(finalized, ProtocolMessage):
        raise ProtocolError("Worker stop_capture did not return a protocol message.")
    return finalized


def _utc_timestamp(now: datetime | None) -> str:
    return utc_timestamp(now)


def _resolve_route_selections(
    *,
    profile: CaptureProfileId,
    discovery: AudioDiscovery,
    microphone_route_id: str | None,
    playback_stream_route_id: str | None,
    output_sink_route_id: str | None,
    interaction: InteractionPort,
) -> RouteSelections:
    microphone = _resolve_audio_route(
        discovery.sources,
        role="microphone",
        flag_name="--mic",
        selected_route_id=microphone_route_id,
        interaction=interaction,
    )
    if profile is CaptureProfileId.IN_PERSON_SINGLE_MIC:
        if playback_stream_route_id is not None or output_sink_route_id is not None:
            raise ValueError("In-person recording accepts --mic only.")
        return RouteSelections(microphone_route_id=microphone.route_id)

    playback_stream = _resolve_playback_stream(
        discovery.playback_streams,
        selected_route_id=playback_stream_route_id,
        interaction=interaction,
    )
    output_sink = _resolve_audio_route(
        discovery.sinks,
        role="output sink",
        flag_name="--output-sink",
        selected_route_id=output_sink_route_id,
        interaction=interaction,
    )
    return RouteSelections(
        microphone_route_id=microphone.route_id,
        playback_stream_route_id=playback_stream.route_id,
        output_sink_route_id=output_sink.route_id,
    )


def _resolve_audio_route(
    routes: tuple[AudioRoute, ...],
    *,
    role: str,
    flag_name: str,
    selected_route_id: str | None,
    interaction: InteractionPort,
) -> AudioRoute:
    if selected_route_id is not None:
        return _find_audio_route(routes, selected_route_id, role)
    if len(routes) == 1:
        return routes[0]
    if not routes:
        raise ValueError(f"No {role} routes were discovered.")
    return _choose_audio_route(
        interaction=interaction,
        topic=f"record.capture_route.{_route_topic_part(role)}",
        role=role,
        flag_name=flag_name,
        routes=routes,
        default_index=_default_audio_route_index(routes),
    )


def _resolve_playback_stream(
    streams: tuple[PlaybackStream, ...],
    *,
    selected_route_id: str | None,
    interaction: InteractionPort,
) -> PlaybackStream:
    active_streams = tuple(stream for stream in streams if stream.active)
    if selected_route_id is not None:
        return _find_active_playback_stream(streams, selected_route_id)
    if not streams:
        raise ValueError("No playback streams were discovered.")
    if not active_streams:
        raise ValueError("No active playback streams were discovered.")
    options = tuple(
        ChoiceOption(
            option_id=stream.route_id,
            label=_format_playback_stream_choice(stream),
            detail=stream.name,
        )
        for stream in active_streams
    )
    chosen = interaction.choose(
        ChoiceRequest(
            topic="record.capture_route.playback_stream",
            options=options,
            default_option_id=options[0].option_id,
        )
    )
    return _find_playback_stream(active_streams, chosen.option_id)


def _find_audio_route(routes: tuple[AudioRoute, ...], route_id: str, role: str) -> AudioRoute:
    for route in routes:
        if route.route_id == route_id:
            return route
    raise ValueError(f"Selected {role} route '{route_id}' was not discovered.")


def _find_playback_stream(streams: tuple[PlaybackStream, ...], route_id: str) -> PlaybackStream:
    for stream in streams:
        if stream.route_id == route_id:
            return stream
    raise ValueError(f"Selected playback stream route '{route_id}' was not discovered.")


def _find_active_playback_stream(
    streams: tuple[PlaybackStream, ...],
    route_id: str,
) -> PlaybackStream:
    stream = _find_playback_stream(streams, route_id)
    if not stream.active:
        raise ValueError(f"Selected playback stream route '{route_id}' is not active.")
    return stream


def _choose_audio_route(
    *,
    interaction: InteractionPort,
    topic: str,
    role: str,
    flag_name: str,
    routes: tuple[AudioRoute, ...],
    default_index: int,
) -> AudioRoute:
    if default_index < 1 or default_index > len(routes):
        raise ValueError(f"Invalid default {role} routes selection '{default_index}'.")
    if not routes:
        raise ValueError(f"Multiple {role} routes were discovered; pass {flag_name}.")
    options = tuple(
        ChoiceOption(
            option_id=route.route_id,
            label=_format_audio_route_choice(route),
            detail=route.name,
        )
        for route in routes
    )
    chosen = interaction.choose(
        ChoiceRequest(
            topic=topic,
            options=options,
            default_option_id=options[default_index - 1].option_id,
        )
    )
    return _find_audio_route(routes, chosen.option_id, role)


def _route_topic_part(role: str) -> str:
    if role == "output sink":
        return "output_sink"
    return role


def _default_audio_route_index(routes: tuple[AudioRoute, ...]) -> int:
    for index, route in enumerate(routes, start=1):
        if route.is_default:
            return index
    return 1


def _wait_for_record_stop(
    interaction: InteractionPort,
    *,
    capture_monitor: _CaptureEventMonitor | None = None,
) -> None:
    interaction.notify(WorkflowNotice(topic="record.stop_prompt_started"))
    while True:
        answer = (
            _read_record_command(
                interaction,
                capture_monitor=capture_monitor,
            )
            .strip()
            .lower()
        )
        if answer in {"q", "quit", "stop"}:
            return
        interaction.notify(WorkflowNotice(topic="record.stop_prompt_continues"))


def _read_record_command(
    interaction: InteractionPort,
    *,
    capture_monitor: _CaptureEventMonitor | None,
) -> str:
    if capture_monitor is None:
        return interaction.request_text(TextRequest(topic="record.stop_command"))

    result_queue: queue.Queue[str | BaseException] = queue.Queue(maxsize=1)

    def read_input() -> None:
        try:
            result_queue.put(interaction.request_text(TextRequest(topic="record.stop_command")))
        except BaseException as exc:
            result_queue.put(exc)

    input_thread = threading.Thread(target=read_input, name="mena-record-input", daemon=True)
    input_thread.start()
    while True:
        terminal_event = capture_monitor.poll_terminal_event()
        if terminal_event is not None:
            return "q"
        try:
            result = result_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        if isinstance(result, BaseException):
            raise result
        return result


def _format_audio_route_choice(route: AudioRoute) -> str:
    base_label = route.description or route.name
    label = base_label
    if route.is_default:
        label = f"{label} [default]"
    if route.node_name and route.node_name != base_label:
        label = f"{label} - {route.node_name}"
    return f"{label} ({route.route_id})"


def _format_playback_stream_choice(stream: PlaybackStream) -> str:
    label = stream.media_name or stream.stream_name or stream.name
    if stream.app_binary_name:
        label = f"{label} - {stream.app_binary_name}"
    return f"{label} ({stream.route_id})"
