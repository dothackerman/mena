import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from mena.interaction import (
    ChoiceOption,
    ChoiceRequest,
    ConfirmationRequest,
    ScriptedInteraction,
    ScriptedResponse,
    TextRequest,
    WorkflowNotice,
)
from mena.profiles import CaptureProfileId, parse_capture_profile
from mena.protocol import ProtocolError, ProtocolMessage, WorkerProcess
from mena.recording_control import (
    RecordCommandInput,
    _CaptureEventMonitor,
    _resolve_route_selections,
    _wait_for_record_stop,
    run_recording_control_workflow,
)
from mena.sessions import create_session
from test_cli import (
    SESSION_ID,
    FailingWorkerProcess,
    FakeWorkerProcess,
    IdenticalHashWorkerProcess,
    NonzeroExitWorkerProcess,
    SilentIdenticalHashWorkerProcess,
    _ambiguous_default_discoverer,
    _ambiguous_discoverer,
    _available_discoverer,
    _discovery_with_playback_streams,
    _EventWorker,
    _finalized_capture_message,
    _monitor_start_payload,
    _segment_payload,
    _unavailable_discoverer,
)


class _BlockingTextInteraction:
    def __init__(self) -> None:
        self.prompt_started = threading.Event()
        self.notices: list[WorkflowNotice] = []

    def choose(self, request: ChoiceRequest) -> ChoiceOption:
        raise AssertionError(f"unexpected choice request {request.topic}")

    def confirm(self, request: ConfirmationRequest) -> bool:
        raise AssertionError(f"unexpected confirmation request {request.topic}")

    def request_text(self, request: TextRequest) -> str:
        self.prompt_started.set()
        time.sleep(5)
        return "q"

    def notify(self, notice: WorkflowNotice) -> None:
        self.notices.append(notice)


def test_recording_control_creates_recorded_session_with_scripted_stop(tmp_path: Path) -> None:
    interaction = ScriptedInteraction(
        [ScriptedResponse(kind="text", topic="record.stop_command", value="q")]
    )

    result = run_recording_control_workflow(
        RecordCommandInput(
            profile=parse_capture_profile("online"),
            language="en",
            name="Planning",
            playback_stream_route_id="pulse:sink-input:151",
        ),
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=FakeWorkerProcess,
        interaction=interaction,
        now=None,
    )

    assert result.exit_code == 0
    assert result.record_path is not None
    record = json.loads(result.record_path.read_text(encoding="utf-8"))
    assert record["status"] == "recorded"
    assert record["name"] == "Planning"
    assert record["capture_plan"]["status"] == "finalized"
    assert record["recording"]["stop_reason"] == "user_stop"
    assert [request.topic for request in interaction.requests] == ["record.stop_command"]
    assert [notice.topic for notice in interaction.notices][-2:] == [
        "record.capture_finalized",
        "record.process_handoff",
    ]


def test_recording_control_uses_interaction_for_ambiguous_routes(tmp_path: Path) -> None:
    interaction = ScriptedInteraction(
        [
            ScriptedResponse(
                kind="choose",
                topic="record.capture_route.microphone",
                value="pipewire:node:32",
            ),
            ScriptedResponse(
                kind="choose",
                topic="record.capture_route.playback_stream",
                value="pulse:sink-input:152",
            ),
            ScriptedResponse(
                kind="choose",
                topic="record.capture_route.output_sink",
                value="pipewire:node:43",
            ),
            ScriptedResponse(kind="text", topic="record.stop_command", value="q"),
        ]
    )

    result = run_recording_control_workflow(
        RecordCommandInput(
            profile=parse_capture_profile("online"),
            language="en",
        ),
        data_root=tmp_path,
        discoverer=_ambiguous_discoverer,
        worker_factory=FakeWorkerProcess,
        interaction=interaction,
        now=None,
    )

    assert result.exit_code == 0
    assert [request.topic for request in interaction.requests[:3]] == [
        "record.capture_route.microphone",
        "record.capture_route.playback_stream",
        "record.capture_route.output_sink",
    ]
    assert FakeWorkerProcess.last_start_payload is not None
    route_ids = [route.route_id for route in FakeWorkerProcess.last_start_payload.routes]
    assert route_ids == ["pipewire:node:32", "pulse:sink-input:152", "pipewire:node:43"]


def test_recording_control_stops_before_capture_when_discovery_unavailable(
    tmp_path: Path,
) -> None:
    interaction = ScriptedInteraction()

    result = run_recording_control_workflow(
        RecordCommandInput(
            profile=parse_capture_profile("online"),
            language="en",
        ),
        data_root=tmp_path,
        discoverer=_unavailable_discoverer,
        worker_factory=FakeWorkerProcess,
        interaction=interaction,
        now=None,
    )

    assert result.exit_code == 1
    assert result.record_path is not None
    record = json.loads(result.record_path.read_text(encoding="utf-8"))
    assert record["discovery"]["status"] == "unavailable"
    assert "capture_metadata" not in record
    assert [notice.topic for notice in interaction.notices][-1] == (
        "record.capture_not_started.discovery_unavailable"
    )


def test_recording_control_rejects_online_route_flags_for_in_person(
    tmp_path: Path,
) -> None:
    interaction = ScriptedInteraction()

    result = run_recording_control_workflow(
        RecordCommandInput(
            profile=parse_capture_profile("in-person"),
            language="en",
            playback_stream_route_id="pulse:sink-input:151",
        ),
        data_root=tmp_path,
        discoverer=lambda _: _available_discoverer(CaptureProfileId.IN_PERSON_SINGLE_MIC),
        worker_factory=FakeWorkerProcess,
        interaction=interaction,
        now=None,
    )

    assert result.exit_code == 1
    assert [notice.topic for notice in interaction.notices][-1] == (
        "record.capture_not_started.error"
    )
    assert interaction.notices[-1].context["error"] == "In-person recording accepts --mic only."


def test_resolve_route_selections_uses_scripted_interaction_for_ambiguous_routes() -> None:
    interaction = ScriptedInteraction(
        [
            ScriptedResponse(
                kind="choose",
                topic="record.capture_route.microphone",
                value="pipewire:node:32",
            ),
            ScriptedResponse(
                kind="choose",
                topic="record.capture_route.playback_stream",
                value="pulse:sink-input:152",
            ),
            ScriptedResponse(
                kind="choose",
                topic="record.capture_route.output_sink",
                value="pipewire:node:43",
            ),
        ]
    )

    selections = _resolve_route_selections(
        profile=CaptureProfileId.ONLINE_DUAL_STREAM,
        discovery=_ambiguous_discoverer(CaptureProfileId.ONLINE_DUAL_STREAM),
        microphone_route_id=None,
        playback_stream_route_id=None,
        output_sink_route_id=None,
        interaction=interaction,
    )

    assert selections.microphone_route_id == "pipewire:node:32"
    assert selections.playback_stream_route_id == "pulse:sink-input:152"
    assert selections.output_sink_route_id == "pipewire:node:43"
    assert [request.topic for request in interaction.requests] == [
        "record.capture_route.microphone",
        "record.capture_route.playback_stream",
        "record.capture_route.output_sink",
    ]
    first_request = cast(ChoiceRequest, interaction.requests[0])
    assert first_request.default_option_id == "pipewire:node:31"


def test_resolve_route_selections_uses_marked_default_route() -> None:
    interaction = ScriptedInteraction(
        [
            ScriptedResponse(kind="choose", topic="record.capture_route.microphone"),
            ScriptedResponse(kind="choose", topic="record.capture_route.playback_stream"),
            ScriptedResponse(kind="choose", topic="record.capture_route.output_sink"),
        ]
    )

    selections = _resolve_route_selections(
        profile=CaptureProfileId.ONLINE_DUAL_STREAM,
        discovery=_ambiguous_default_discoverer(CaptureProfileId.ONLINE_DUAL_STREAM),
        microphone_route_id=None,
        playback_stream_route_id=None,
        output_sink_route_id=None,
        interaction=interaction,
    )

    assert selections.microphone_route_id == "pipewire:node:32"
    assert selections.playback_stream_route_id == "pulse:sink-input:151"
    assert selections.output_sink_route_id == "pipewire:node:43"


def test_resolve_route_selections_prompts_for_single_playback_stream() -> None:
    interaction = ScriptedInteraction(
        [ScriptedResponse(kind="choose", topic="record.capture_route.playback_stream")]
    )

    selections = _resolve_route_selections(
        profile=CaptureProfileId.ONLINE_DUAL_STREAM,
        discovery=_available_discoverer(CaptureProfileId.ONLINE_DUAL_STREAM),
        microphone_route_id=None,
        playback_stream_route_id=None,
        output_sink_route_id=None,
        interaction=interaction,
    )

    assert selections.playback_stream_route_id == "pulse:sink-input:151"
    assert [request.topic for request in interaction.requests] == [
        "record.capture_route.playback_stream"
    ]


def test_resolve_route_selections_rejects_missing_playback_streams() -> None:
    with pytest.raises(ValueError, match="No playback streams were discovered"):
        _resolve_route_selections(
            profile=CaptureProfileId.ONLINE_DUAL_STREAM,
            discovery=_discovery_with_playback_streams(),
            microphone_route_id=None,
            playback_stream_route_id=None,
            output_sink_route_id=None,
            interaction=ScriptedInteraction(),
        )


def test_resolve_route_selections_rejects_inactive_explicit_playback_stream() -> None:
    discovery = _discovery_with_playback_streams(
        replace(
            _available_discoverer(CaptureProfileId.ONLINE_DUAL_STREAM).playback_streams[0],
            active=False,
        )
    )

    with pytest.raises(ValueError, match="Selected playback stream route .* is not active"):
        _resolve_route_selections(
            profile=CaptureProfileId.ONLINE_DUAL_STREAM,
            discovery=discovery,
            microphone_route_id=None,
            playback_stream_route_id="pulse:sink-input:151",
            output_sink_route_id=None,
            interaction=ScriptedInteraction(),
        )


def test_capture_event_monitor_records_notices_and_degradations(tmp_path: Path) -> None:
    created_session = create_session(
        profile=parse_capture_profile("online"),
        language="en",
        data_root=tmp_path,
    )
    worker = _EventWorker(
        [
            ProtocolMessage(
                type="segments_flushed",
                session_id=SESSION_ID,
                payload={
                    "segments": [
                        _segment_payload(source_id="local_mic", index=1),
                        _segment_payload(source_id="system_audio", index=1),
                    ]
                },
            ),
            ProtocolMessage(
                type="capture_status",
                session_id=SESSION_ID,
                payload={
                    "status": "recording",
                    "elapsed_seconds": 12.5,
                    "sealed_segment_count": 1,
                },
            ),
            ProtocolMessage(
                type="capture_route_event",
                session_id=SESSION_ID,
                payload={
                    "event": "degraded",
                    "code": "dedicated_route_runtime_failed",
                    "message": "Selected playback stream ended.",
                    "source_id": "system_audio",
                    "occurred_at": None,
                    "elapsed_seconds": 54.0,
                    "segment_index": 1,
                    "route_id": "pipewire:node:42",
                    "route_label": "Headphones",
                    "capture_mode": "system_mix_remote",
                },
            ),
            _finalized_capture_message(),
        ]
    )
    interaction = ScriptedInteraction()
    monitor = _CaptureEventMonitor(
        cast(WorkerProcess, worker),
        record_path=created_session.record_path,
        start_payload=_monitor_start_payload(),
        interaction=interaction,
    )

    monitor.start()
    finalized = monitor.wait_for_finalized()

    record = json.loads(created_session.record_path.read_text(encoding="utf-8"))
    assert finalized.type == "capture_finalized"
    assert record["capture_metadata"]["segment_count"] == 2
    assert record["degradations"][0]["capture_mode"] == "system_mix_remote"
    assert record["degradations"][0]["code"] == "dedicated_route_runtime_failed"
    assert [notice.topic for notice in interaction.notices] == [
        "record.segment_flushed",
        "record.segment_flushed",
        "record.capture_status",
        "record.capture_route_event",
    ]
    route_event = interaction.notices[-1]
    assert route_event.context["event"] == "degraded"
    assert route_event.context["code"] == "dedicated_route_runtime_failed"
    assert route_event.context["source_id"] == "system_audio"
    assert route_event.context["selected_sources"] == ["local_mic", "system_audio"]


def test_wait_for_record_stop_continues_after_invalid_command() -> None:
    interaction = ScriptedInteraction(
        [
            ScriptedResponse(kind="text", topic="record.stop_command", value="later"),
            ScriptedResponse(kind="text", topic="record.stop_command", value=""),
            ScriptedResponse(kind="text", topic="record.stop_command", value="q"),
        ]
    )

    _wait_for_record_stop(interaction)

    assert [notice.topic for notice in interaction.notices] == [
        "record.stop_prompt_started",
        "record.stop_prompt_continues",
        "record.stop_prompt_continues",
    ]


def test_wait_for_record_stop_surfaces_background_capture_error(tmp_path: Path) -> None:
    worker = _EventWorker(
        [
            ProtocolMessage(
                type="error",
                session_id=SESSION_ID,
                payload={
                    "code": "capture_flush_failed",
                    "message": "disk full",
                    "recoverable": False,
                    "details": {},
                },
            )
        ]
    )
    created_session = create_session(
        profile=parse_capture_profile("online"),
        language="en",
        data_root=tmp_path,
    )
    interaction = _BlockingTextInteraction()
    monitor = _CaptureEventMonitor(
        cast(WorkerProcess, worker),
        record_path=created_session.record_path,
        start_payload=_monitor_start_payload(),
        interaction=interaction,
    )

    monitor.start()

    with pytest.raises(ProtocolError, match="disk full"):
        _wait_for_record_stop(interaction, capture_monitor=monitor)
    assert interaction.prompt_started.is_set()


def test_recording_control_finalizes_failed_on_identical_online_source_hashes(
    tmp_path: Path,
) -> None:
    interaction = ScriptedInteraction(
        [ScriptedResponse(kind="text", topic="record.stop_command", value="q")]
    )

    result = run_recording_control_workflow(
        RecordCommandInput(
            profile=parse_capture_profile("online"),
            language="en",
            playback_stream_route_id="pulse:sink-input:151",
        ),
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=IdenticalHashWorkerProcess,
        interaction=interaction,
        now=None,
    )

    assert result.exit_code == 1
    assert result.record_path is not None
    record = json.loads(result.record_path.read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert record["recording"]["finalization"]["status"] == "failed"
    assert "source independence failed" in record["recording"]["finalization"]["error"]
    notices = [notice.topic for notice in interaction.notices]
    assert notices[-1] == "record.finalized_status_error"
    assert "record.process_handoff" not in notices


def test_recording_control_allows_identical_silent_online_source_hashes(
    tmp_path: Path,
) -> None:
    interaction = ScriptedInteraction(
        [ScriptedResponse(kind="text", topic="record.stop_command", value="q")]
    )

    result = run_recording_control_workflow(
        RecordCommandInput(
            profile=parse_capture_profile("online"),
            language="en",
            playback_stream_route_id="pulse:sink-input:151",
        ),
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=SilentIdenticalHashWorkerProcess,
        interaction=interaction,
        now=None,
    )

    assert result.exit_code == 0
    assert result.record_path is not None
    record = json.loads(result.record_path.read_text(encoding="utf-8"))
    assert record["status"] == "recorded"
    assert record["recording"]["finalization"]["status"] == "finalized"
    assert "source independence failed" not in json.dumps(record)
    assert [notice.topic for notice in interaction.notices][-1] == "record.process_handoff"


def test_recording_control_persists_worker_spawn_failure(tmp_path: Path) -> None:
    def _spawn_fails(*, session_id: str, session_dir: Path) -> FakeWorkerProcess:
        raise OSError("worker process limit reached")

    interaction = ScriptedInteraction()

    result = run_recording_control_workflow(
        RecordCommandInput(
            profile=parse_capture_profile("online"),
            language="en",
            playback_stream_route_id="pulse:sink-input:151",
        ),
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=_spawn_fails,
        interaction=interaction,
        now=None,
    )

    assert result.exit_code == 1
    assert result.record_path is not None
    record = json.loads(result.record_path.read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert record["recording"]["stop_reason"] == "error"
    assert record["recording"]["finalization"]["status"] == "failed"
    assert record["recording"]["finalization"]["error"] == "worker process limit reached"
    assert record["failures"][0]["message"] == "worker process limit reached"
    assert record["failures"][0]["code"] == "capture_failed"
    assert "capture_metadata" not in record
    last = interaction.notices[-1]
    assert last.topic == "record.capture_failed"
    assert last.context["error"] == "worker process limit reached"


def test_recording_control_rejects_nonzero_worker_exit(tmp_path: Path) -> None:
    interaction = ScriptedInteraction(
        [ScriptedResponse(kind="text", topic="record.stop_command", value="q")]
    )

    result = run_recording_control_workflow(
        RecordCommandInput(
            profile=parse_capture_profile("online"),
            language="en",
            playback_stream_route_id="pulse:sink-input:151",
        ),
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=NonzeroExitWorkerProcess,
        interaction=interaction,
        now=None,
    )

    assert result.exit_code == 1
    assert result.record_path is not None
    record = json.loads(result.record_path.read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert record["recording"]["finalization"]["error"] == "Worker exited with status 3."
    assert record["failures"][0]["code"] == "capture_failed"
    assert "capture_metadata" not in record
    assert interaction.notices[-1].topic == "record.capture_failed"


def test_recording_control_reports_worker_handshake_failure(tmp_path: Path) -> None:
    interaction = ScriptedInteraction()

    result = run_recording_control_workflow(
        RecordCommandInput(
            profile=parse_capture_profile("online"),
            language="en",
            playback_stream_route_id="pulse:sink-input:151",
        ),
        data_root=tmp_path,
        discoverer=_available_discoverer,
        worker_factory=FailingWorkerProcess,
        interaction=interaction,
        now=None,
    )

    assert result.exit_code == 1
    assert result.record_path is not None
    record = json.loads(result.record_path.read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert record["recording"]["finalization"]["error"] == "worker refused handshake"
    assert record["failures"][0]["message"] == "worker refused handshake"
    assert "capture_metadata" not in record
    last = interaction.notices[-1]
    assert last.topic == "record.capture_failed"
    assert last.context["error"] == "worker refused handshake"


def test_capture_event_monitor_surfaces_background_error(tmp_path: Path) -> None:
    created_session = create_session(
        profile=parse_capture_profile("online"),
        language="en",
        data_root=tmp_path,
    )
    worker = _EventWorker(
        [
            ProtocolMessage(
                type="error",
                session_id=SESSION_ID,
                payload={
                    "code": "capture_flush_failed",
                    "message": "disk full",
                    "recoverable": False,
                    "details": {},
                },
            )
        ]
    )
    monitor = _CaptureEventMonitor(
        cast(WorkerProcess, worker),
        record_path=created_session.record_path,
        start_payload=_monitor_start_payload(),
        interaction=ScriptedInteraction(),
    )

    monitor.start()

    try:
        monitor.wait_for_finalized()
    except ProtocolError as exc:
        assert str(exc) == "disk full"
    else:
        raise AssertionError("expected ProtocolError")
