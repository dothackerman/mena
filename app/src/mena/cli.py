"""CLI adapter for the mena PoC."""

from __future__ import annotations

import argparse
import hashlib
import json
import queue
import subprocess
import sys
import threading
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from mena.artifacts import (
    ArtifactRecord,
    ArtifactStore,
    ArtifactStoreError,
    FileSystemArtifactStore,
)
from mena.capture_planning import (
    CapturePlanningError,
    RouteSelections,
    build_start_capture_payload,
)
from mena.correction_overlay import (
    CorrectionOverlayContractError,
    resolve_active_correction_overlay,
)
from mena.correction_workflow import (
    CorrectionWorkflowError,
    PlaybackPlayer,
    run_correction_workflow,
)
from mena.diarization import (
    DIARIZATION_STAGE,
    DiarizationContext,
    DiarizationError,
    DiarizationProvider,
    PyannoteCommunityDiarizationProvider,
    run_diarization_stage,
)
from mena.discovery import (
    AudioDiscovery,
    AudioRoute,
    DiscoveryStatus,
    PlaybackStream,
    discover_audio_routes,
    format_discovery,
)
from mena.export import (
    SummaryMarkdownExportError,
    export_summary_markdown,
)
from mena.manual_notes import ManualNotesIngestionError, ingest_manual_notes_file
from mena.merge_timing import (
    MERGE_TIMING_STAGE,
    MERGED_TRANSCRIPT_ARTIFACT_TYPE,
    MergedTranscriptArtifact,
    MergeTimingContractError,
    MergeTimingError,
    run_merge_timing_stage,
    run_room_mic_merge_timing_stage,
)
from mena.processing import (
    PROCESSING_STATE_ARTIFACT_TYPE,
    ProcessingStageLedger,
    ProcessingStageState,
    ProcessingStateError,
)
from mena.profiles import (
    CaptureProfile,
    CaptureProfileId,
    CaptureProfileSelection,
    DiarisationPolicy,
    capture_profile_choices,
    get_capture_profile,
    parse_capture_profile,
)
from mena.protocol import (
    CaptureFinalizedPayload,
    CaptureStatusPayload,
    ErrorPayload,
    ProtocolError,
    ProtocolMessage,
    SegmentFlushedPayload,
    StartCapturePayload,
    WorkerExitedError,
    WorkerProcess,
)
from mena.review_flagging import (
    CONFIDENCE_FLAGGING_STAGE,
    ReviewFlaggingError,
    run_confidence_flagging_stage,
)
from mena.runtime_profile import (
    PROCESSING_RUNTIME_STAGES,
    SUMMARIZATION_RUNTIME_STAGES,
    RuntimeProfileSelection,
    production_runtime_required,
    runtime_diarization_factory,
    runtime_semantic_factory,
    runtime_summarization_provider,
    runtime_transcription_factory,
    select_runtime_profile,
    store_runtime_profile_provenance,
)
from mena.semantic_coherence import (
    SEMANTIC_COHERENCE_STAGE,
    OllamaSemanticCoherenceProvider,
    SemanticCoherenceError,
    SemanticCoherenceProvider,
    run_semantic_coherence_stage,
)
from mena.session_integrity import (
    SessionIntegrityError,
    load_verified_session_bundle,
    verify_session_bundle,
)
from mena.sessions import (
    SESSION_RECORD_FILENAME,
    append_session_failure,
    create_session,
    read_session_record,
    update_session_capture_metadata,
    update_session_capture_plan,
    update_session_discovery,
    update_session_record,
)
from mena.settings import AppSettings, SettingsError, validate_summary_language
from mena.setup import (
    CommandIdentity,
    CommandRunner,
    HardwareFacts,
    ReadinessItem,
    SubprocessCommandRunner,
    build_runtime_profile_state,
    detect_local_installation_state,
    evaluate_setup,
    execute_setup_plan,
    format_setup_report,
    get_alpha_local_profile,
    persist_runtime_profile_state,
)
from mena.summarization import (
    SummarizationError,
    SummarizationPolicy,
    run_summarization_stage,
)
from mena.transcription import (
    TRANSCRIPTION_STAGE,
    FasterWhisperTranscriptionProvider,
    TranscriptionError,
    TranscriptionProvider,
    run_transcription_stage,
)

COMMANDS = (
    "record",
    "setup",
    "list",
    "show",
    "process",
    "correct",
    "summarize",
    "export",
    "inspect",
    "resume",
)
WORKER_EXIT_TIMEOUT_SECONDS = 30.0
PROCESSING_PIPELINE_STAGES = frozenset(
    {
        TRANSCRIPTION_STAGE,
        DIARIZATION_STAGE,
        MERGE_TIMING_STAGE,
        CONFIDENCE_FLAGGING_STAGE,
        SEMANTIC_COHERENCE_STAGE,
    }
)


ProviderFactory = Callable[[], Any]
ChoiceT = TypeVar("ChoiceT")


class ProviderValidationError(TypeError):
    """Raised when a configured processing provider does not satisfy its protocol."""


@dataclass(frozen=True)
class SourceAudioInput:
    source_id: str
    index: int
    path: Path
    artifact: ArtifactRecord


@dataclass(frozen=True)
class CaptureSegmentInput:
    source_id: str
    index: int
    path: Path
    artifact: ArtifactRecord
    sha256: str


class _CaptureEventMonitor:
    """Drain worker capture events while the foreground prompt waits for stop."""

    def __init__(
        self,
        worker: WorkerProcess,
        *,
        record_path: Path,
        start_payload: StartCapturePayload,
    ) -> None:
        self.worker = worker
        self.record_path = record_path
        self.start_payload = start_payload
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
            self.segments.append(dict(message.payload))
            update_session_capture_metadata(
                self.record_path,
                _capture_metadata_from_segments(
                    self.start_payload,
                    self.segments,
                ),
            )
            print(f"Sealed {segment.source_id} segment {segment.index}.")
            return True
        if message.type == "capture_status":
            status = CaptureStatusPayload.from_dict(message.payload)
            print(
                "Recording: "
                f"{int(status.elapsed_seconds)}s elapsed, "
                f"{status.sealed_segment_count} sealed segment(s)."
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mena",
        description="Privacy-first local meeting notes PoC.",
    )
    parser.add_argument("--version", action="store_true", help="Show the package version.")

    subparsers = parser.add_subparsers(dest="command")
    record_parser = subparsers.add_parser("record", help="Create a recording session.")
    record_parser.add_argument(
        "--profile",
        choices=capture_profile_choices(),
        help="Capture profile for this recording session.",
    )
    record_parser.add_argument("--name", help="Friendly session name for human reference.")
    record_parser.add_argument(
        "--language",
        default=None,
        help="Meeting language to persist as the session language.",
    )
    record_parser.add_argument("--mic", default=None, help="Microphone route id to capture.")
    record_parser.add_argument(
        "--playback-stream",
        default=None,
        help="Playback stream route id to capture for online meetings.",
    )
    record_parser.add_argument(
        "--output-sink",
        default=None,
        help="Output sink route id used to keep the meeting audible.",
    )
    record_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Create a session and print read-only audio discovery without starting capture.",
    )
    record_parser.add_argument(
        "--worker-exit-timeout",
        type=float,
        default=WORKER_EXIT_TIMEOUT_SECONDS,
        help="Seconds to wait for capture worker teardown after stop_capture.",
    )
    record_parser.set_defaults(command="record")

    setup_parser = subparsers.add_parser("setup", help="Run Linux alpha setup/preflight.")
    setup_parser.add_argument(
        "--check",
        action="store_true",
        help="Only evaluate local install/provider readiness; do not mutate.",
    )
    setup_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm planned automated setup actions without prompting.",
    )
    setup_parser.set_defaults(command="setup")

    for command in COMMANDS[1:]:
        if command == "setup":
            continue
        subparser = subparsers.add_parser(command)
        if command in {"correct", "process", "resume", "summarize", "export", "inspect"}:
            subparser.add_argument(
                "session_path",
                help="Path to the session directory or session.json.",
            )
        if command in {"process", "resume", "summarize"}:
            subparser.add_argument(
                "--allow-runtime-fallback",
                action="store_true",
                help=(
                    "Allow explicit one-run fallback if the accepted runtime profile is "
                    "unavailable."
                ),
            )

        if command == "export":
            subparser.add_argument(
                "--summary-artifact-id",
                dest="summary_artifact_id",
                default=None,
                help="Optional explicit summary artifact id to export.",
            )
        if command == "summarize":
            subparser.add_argument(
                "--manual-notes",
                dest="manual_notes",
                help="Optional path to manual notes file.",
            )
            subparser.add_argument(
                "--language",
                default=None,
                help="Language code for the summary model prompt.",
            )
        subparser.set_defaults(command=command)

    return parser


DiscoveryFn = Callable[..., AudioDiscovery]
WorkerFactory = Callable[..., Any]


def main(
    argv: Sequence[str] | None = None,
    *,
    data_root: Path | None = None,
    discoverer: DiscoveryFn = discover_audio_routes,
    worker_factory: WorkerFactory = WorkerProcess,
    now: datetime | None = None,
    run_id: str | None = None,
    transcription_provider_factory: ProviderFactory = FasterWhisperTranscriptionProvider,
    diarization_provider_factory: ProviderFactory = PyannoteCommunityDiarizationProvider,
    semantic_coherence_provider_factory: ProviderFactory = OllamaSemanticCoherenceProvider,
    settings: AppSettings | None = None,
    config_path: Path | None = None,
    input_fn: Callable[[str], str] | None = None,
    playback_player: PlaybackPlayer | None = None,
    setup_runner: CommandRunner | None = None,
    setup_command_identity: CommandIdentity | None = None,
    setup_hardware: HardwareFacts | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        from mena import __version__

        print(__version__)
        return 0

    if args.command is None:
        parser.print_help()
        return 0

    try:
        app_settings = settings or AppSettings.from_env(config_path=config_path)
    except SettingsError as exc:
        print(f"Cannot load configuration: {exc}")
        return 1

    effective_semantic_coherence_provider_factory = semantic_coherence_provider_factory

    if args.command == "setup":
        return _run_setup_command(
            check_only=args.check,
            assume_yes=args.yes,
            settings=app_settings,
            input_fn=input_fn,
            runner=setup_runner,
            command_identity=setup_command_identity,
            hardware=setup_hardware,
        )

    if args.command == "record":
        try:
            profile = _resolve_record_profile(args.profile, input_fn=input_fn)
            language = _resolve_record_language(
                args.language,
                default_language=app_settings.summary.language,
                input_fn=input_fn,
                dry_run=args.dry_run,
            )
        except ValueError as exc:
            print(f"Cannot start recording: {exc}")
            return 1
        except (EOFError, KeyboardInterrupt):
            print("Recording setup cancelled.")
            return 1
        created_session = create_session(
            profile=profile,
            language=language,
            name=args.name,
            data_root=data_root,
        )
        print(f"Created session {created_session.record.id}")
        print(f"Session record: {created_session.record_path}")
        discovery = discoverer(profile.id)
        update_session_discovery(created_session.record_path, discovery.to_dict())
        print(format_discovery(discovery))
        if args.dry_run:
            return 0
        if discovery.status is not DiscoveryStatus.AVAILABLE:
            print("Capture not started: audio discovery is unavailable.")
            return 1
        try:
            route_selections = _resolve_route_selections(
                profile=profile.id,
                discovery=discovery,
                microphone_route_id=args.mic,
                playback_stream_route_id=args.playback_stream,
                output_sink_route_id=args.output_sink,
                input_fn=input_fn,
            )
            start_payload = build_start_capture_payload(
                profile=profile.id,
                language=language,
                discovery=discovery,
                selections=route_selections,
            )
        except (CapturePlanningError, ValueError) as exc:
            print(f"Capture not started: {exc}")
            return 1
        if _record_input(input_fn) is None:
            print("Capture not started: recording requires an interactive terminal to stop.")
            return 1
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
        with worker_factory(
            session_id=created_session.record.id,
            session_dir=created_session.bundle_path,
        ) as worker:
            capture_monitor = _capture_event_monitor(
                worker,
                record_path=created_session.record_path,
                start_payload=start_payload,
            )
            interrupted_stop = False
            try:
                worker.handshake()
                print(
                    "Recording notice: you are responsible for obtaining consent "
                    "from meeting participants."
                )
                worker.start_capture(start_payload)
                if capture_monitor is not None:
                    capture_monitor.start()
                _wait_for_record_stop(input_fn)
                finalized = _finalize_recording(worker, capture_monitor)
                exit_code = worker.wait(timeout=args.worker_exit_timeout)
                if exit_code != 0:
                    raise WorkerExitedError(f"Worker exited with status {exit_code}.")
            except (EOFError, KeyboardInterrupt):
                print("Recording stop requested; finalizing capture.")
                interrupted_stop = True
                finalized = _finalize_recording(worker, capture_monitor)
                exit_code = worker.wait(timeout=args.worker_exit_timeout)
                if exit_code != 0:
                    message = f"Worker exited with status {exit_code}."
                    _persist_capture_failure(
                        record_path=created_session.record_path,
                        start_payload=start_payload,
                        started_at=started_at,
                        error_message=message,
                        now=now,
                    )
                    print(f"Capture failed: {message}")
                    return 1
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
                print(f"Capture failed: {exc}")
                return 1
        finalized_payload = CaptureFinalizedPayload.from_dict(finalized.payload)
        completed_at = finalized_payload.completed_at or _utc_timestamp(now)
        stop_reason = "interrupted" if interrupted_stop else finalized_payload.stop_reason
        update_session_capture_metadata(
            created_session.record_path,
            finalized_payload.capture_metadata,
        )
        update_session_record(
            created_session.record_path,
            updates={
                "status": finalized_payload.status,
                "recording": _recording_lifecycle(
                    started_at=started_at,
                    stopped_at=completed_at,
                    stop_reason=stop_reason,
                    finalization_status=(
                        "finalized"
                        if finalized_payload.status == "recorded"
                        else finalized_payload.status
                    ),
                    completed_at=completed_at,
                    error=finalized_payload.error,
                ),
                "capture_plan": _capture_plan_from_start_payload(
                    start_payload,
                    status=(
                        "finalized"
                        if finalized_payload.status == "recorded"
                        else finalized_payload.status
                    ),
                ),
            },
        )
        print("Capture finalized")
        print(f"To process this recording, run: mena process {created_session.record_path}")
        return 0

    if args.command == "correct":
        try:
            run_correction_workflow(
                session_path=Path(args.session_path),
                now=now,
                input_fn=input if input_fn is None else input_fn,
                playback_player=playback_player,
            )
        except (ArtifactStoreError, CorrectionWorkflowError) as exc:
            print(f"Cannot run correction workflow: {exc}")
            return 1
        except (EOFError, KeyboardInterrupt):
            print("Correction workflow cancelled.")
            return 1
        return 0

    if args.command == "summarize":
        try:
            return _run_summarize_command(
                session_path=Path(args.session_path),
                manual_notes=args.manual_notes,
                language=(
                    None
                    if args.language is None
                    else validate_summary_language(args.language, "--language")
                ),
                settings=app_settings,
                setup_runner=setup_runner,
                setup_command_identity=setup_command_identity,
                setup_hardware=setup_hardware,
                allow_runtime_fallback=args.allow_runtime_fallback,
            )
        except (
            ArtifactStoreError,
            CorrectionOverlayContractError,
            ManualNotesIngestionError,
            SettingsError,
            SummarizationError,
            ValueError,
        ) as exc:
            print(f"Cannot summarize session: {exc}")
            return 1
        except (EOFError, KeyboardInterrupt):
            print("Summarization cancelled.")
            return 1
    if args.command == "export":
        try:
            return _run_export_command(
                session_path=Path(args.session_path),
                summary_artifact_id=args.summary_artifact_id,
            )
        except (
            ArtifactStoreError,
            SummaryMarkdownExportError,
            SessionIntegrityError,
            OSError,
            ValueError,
        ) as exc:
            print(f"Cannot export session summary markdown: {exc}")
            return 1

    if args.command == "inspect":
        try:
            return _run_inspect_command(session_path=Path(args.session_path))
        except (
            ArtifactStoreError,
            ValueError,
            OSError,
        ) as exc:
            print(f"Cannot inspect session: {exc}")
            return 1

    if args.command in {"process", "resume"}:
        try:
            if args.command == "process":
                invocation_run_id = run_id or str(uuid4())
                invocation_attempt = 1
            else:
                session_dir, session_record = _resolve_session(Path(args.session_path))
                session_id = _extract_session_id(session_record)
                store = FileSystemArtifactStore(session_id=session_id, session_dir=session_dir)
                resumable = _latest_resumable_processing_state(store, session_id=session_id)
                if resumable is None:
                    blocked = _latest_non_retryable_processing_failure(store, session_id=session_id)
                    if blocked is not None:
                        reason = (
                            blocked.error.message
                            if blocked.error is not None
                            else "Unknown processing failure."
                        )
                        code = (
                            blocked.error.code
                            if blocked.error is not None and blocked.error.code is not None
                            else None
                        )
                        context = f" (code={code})" if code else ""
                        print(
                            "Latest processing failure is non-retryable at run "
                            f"'{blocked.run_id}', stage '{blocked.stage}', "
                            f"attempt {blocked.attempt}: "
                            f"{reason}{context}"
                        )
                    else:
                        print("No retryable processing failure found to resume.")
                    return 1
                invocation_run_id = resumable.run_id
                invocation_attempt = resumable.attempt + 1

            return _run_processing_command(
                session_path=Path(args.session_path),
                run_id=invocation_run_id,
                attempt=invocation_attempt,
                now=now,
                transcription_provider_factory=transcription_provider_factory,
                diarization_provider_factory=diarization_provider_factory,
                semantic_coherence_provider_factory=effective_semantic_coherence_provider_factory,
                settings=app_settings,
                setup_runner=setup_runner,
                setup_command_identity=setup_command_identity,
                setup_hardware=setup_hardware,
                allow_runtime_fallback=args.allow_runtime_fallback,
            )
        except (
            ArtifactStoreError,
            ProcessingStateError,
            ValueError,
            TranscriptionError,
            DiarizationError,
            MergeTimingError,
            ReviewFlaggingError,
            SemanticCoherenceError,
            ProviderValidationError,
            OSError,
        ) as exc:
            print(f"Cannot run processing workflow: {exc}")
            return 1
        except (EOFError, KeyboardInterrupt):
            print("Processing workflow interrupted.")
            return 1

    print(f"Command '{args.command}' is not implemented yet.")
    return 0


def _capture_plan_from_start_payload(
    payload: StartCapturePayload,
    *,
    status: str,
) -> dict[str, object]:
    plan = payload.to_dict()
    plan["version"] = 1
    plan["status"] = status
    return plan


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
        summaries.append(
            {
                "source_id": source_id,
                "route_id": None if route is None else route.route_id,
                "route_label": None if route is None else route.label,
                "channel_layout": channel_layout,
                "segment_count": len(source_segments),
            }
        )
    return summaries


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
) -> _CaptureEventMonitor | None:
    if isinstance(worker, WorkerProcess):
        return _CaptureEventMonitor(
            worker,
            record_path=record_path,
            start_payload=start_payload,
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
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    return timestamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolve_record_profile(
    raw_profile: str | None,
    *,
    input_fn: Callable[[str], str] | None,
) -> CaptureProfileSelection:
    if raw_profile is not None:
        return parse_capture_profile(raw_profile)
    reader = _record_input(input_fn)
    if reader is None:
        raise ValueError("Capture profile is required in non-interactive mode; pass --profile.")
    print("Capture profiles:")
    print("  1. online")
    print("  2. in-person")
    answer = reader("Select capture profile [online]: ").strip() or "online"
    if answer == "1":
        answer = "online"
    elif answer == "2":
        answer = "in-person"
    return parse_capture_profile(answer)


def _resolve_record_language(
    raw_language: str | None,
    *,
    default_language: str,
    input_fn: Callable[[str], str] | None,
    dry_run: bool,
) -> str:
    if raw_language is not None:
        return validate_summary_language(raw_language, "--language")
    if dry_run:
        return default_language
    reader = _record_input(input_fn)
    if reader is None:
        raise ValueError("Meeting language is required before recording; pass --language.")
    answer = reader(f"Meeting language [{default_language}]: ").strip() or default_language
    return validate_summary_language(answer, "meeting language")


def _resolve_route_selections(
    *,
    profile: CaptureProfileId,
    discovery: AudioDiscovery,
    microphone_route_id: str | None,
    playback_stream_route_id: str | None,
    output_sink_route_id: str | None,
    input_fn: Callable[[str], str] | None,
) -> RouteSelections:
    microphone = _resolve_audio_route(
        discovery.sources,
        role="microphone",
        flag_name="--mic",
        selected_route_id=microphone_route_id,
        input_fn=input_fn,
    )
    if profile is CaptureProfileId.IN_PERSON_SINGLE_MIC:
        if playback_stream_route_id is not None or output_sink_route_id is not None:
            raise ValueError("In-person recording accepts --mic only.")
        return RouteSelections(microphone_route_id=microphone.route_id)

    playback_stream = _resolve_playback_stream(
        discovery.playback_streams,
        selected_route_id=playback_stream_route_id,
        input_fn=input_fn,
    )
    output_sink = _resolve_audio_route(
        discovery.sinks,
        role="output sink",
        flag_name="--output-sink",
        selected_route_id=output_sink_route_id,
        input_fn=input_fn,
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
    input_fn: Callable[[str], str] | None,
) -> AudioRoute:
    if selected_route_id is not None:
        return _find_audio_route(routes, selected_route_id, role)
    if len(routes) == 1:
        return routes[0]
    if not routes:
        raise ValueError(f"No {role} routes were discovered.")
    reader = _record_input(input_fn)
    if reader is None:
        raise ValueError(f"Multiple {role} routes were discovered; pass {flag_name}.")
    return _prompt_choice(
        title=f"{role.title()} routes",
        choices=routes,
        formatter=_format_audio_route_choice,
        input_fn=reader,
    )


def _resolve_playback_stream(
    streams: tuple[PlaybackStream, ...],
    *,
    selected_route_id: str | None,
    input_fn: Callable[[str], str] | None,
) -> PlaybackStream:
    if selected_route_id is not None:
        return _find_playback_stream(streams, selected_route_id)
    if len(streams) == 1:
        return streams[0]
    if not streams:
        raise ValueError("No playback streams were discovered.")
    reader = _record_input(input_fn)
    if reader is None:
        raise ValueError("Multiple playback streams were discovered; pass --playback-stream.")
    return _prompt_choice(
        title="Playback streams",
        choices=streams,
        formatter=_format_playback_stream_choice,
        input_fn=reader,
    )


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


def _prompt_choice(
    *,
    title: str,
    choices: tuple[ChoiceT, ...],
    formatter: Callable[[ChoiceT], str],
    input_fn: Callable[[str], str],
) -> ChoiceT:
    print(f"{title}:")
    for index, choice in enumerate(choices, start=1):
        print(f"  {index}. {formatter(choice)}")
    answer = input_fn(f"Select {title.lower()} [1]: ").strip() or "1"
    if not answer.isdigit():
        raise ValueError(f"Invalid {title.lower()} selection '{answer}'.")
    selected_index = int(answer)
    if selected_index < 1 or selected_index > len(choices):
        raise ValueError(f"Invalid {title.lower()} selection '{answer}'.")
    return choices[selected_index - 1]


def _record_input(input_fn: Callable[[str], str] | None) -> Callable[[str], str] | None:
    if input_fn is not None:
        return input_fn
    if sys.stdin.isatty():
        return input
    return None


def _wait_for_record_stop(input_fn: Callable[[str], str] | None) -> None:
    reader = _record_input(input_fn)
    if reader is None:
        raise RuntimeError("Recording requires an interactive terminal to stop.")
    print("Recording. Press q to stop.")
    answer = reader("record> ").strip().lower()
    if answer not in {"q", "quit", "stop"}:
        raise ValueError("Recording stop command must be q, quit, or stop.")


def _format_audio_route_choice(route: AudioRoute) -> str:
    label = route.description or route.name
    return f"{label} ({route.route_id})"


def _format_playback_stream_choice(stream: PlaybackStream) -> str:
    label = stream.media_name or stream.stream_name or stream.name
    if stream.app_binary_name:
        label = f"{label} - {stream.app_binary_name}"
    return f"{label} ({stream.route_id})"


def _run_setup_command(
    *,
    check_only: bool,
    assume_yes: bool,
    settings: AppSettings,
    input_fn: Callable[[str], str] | None,
    runner: CommandRunner | None,
    command_identity: CommandIdentity | None,
    hardware: HardwareFacts | None,
) -> int:
    command_runner = runner or SubprocessCommandRunner()
    profile = get_alpha_local_profile(settings.local_llm.ollama_model)
    try:
        state = detect_local_installation_state(
            settings,
            runner=command_runner,
            command_identity=command_identity,
            hardware=hardware,
        )
    except KeyboardInterrupt:
        print("Setup interrupted; local provider readiness is incomplete.")
        return 1
    evaluation = evaluate_setup(profile, state)
    print(format_setup_report(evaluation), end="")

    if check_only:
        return 0 if evaluation.ready else 1
    if evaluation.ready:
        accepted_state = build_runtime_profile_state(evaluation)
        state_path = persist_runtime_profile_state(accepted_state, paths=settings.paths)
        print(f"Accepted runtime profile state: {state_path}")
        return 0
    if not evaluation.plan.actions:
        return 1
    if _readiness_item_failed(evaluation.readiness, "command:mena"):
        print("Setup actions were not run because this is not the installed product command.")
        return 1
    if not any(action.mode == "automated" for action in evaluation.plan.actions):
        print("Setup actions were not run because only manual actions remain.")
        return 1

    prompt_fn = input if input_fn is None else input_fn
    if not assume_yes:
        try:
            answer = prompt_fn("Proceed with automated setup actions? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            print("Setup interrupted; local provider readiness is incomplete.")
            return 1
        if answer.strip().lower() not in {"y", "yes"}:
            print("Setup declined; local provider readiness is incomplete.")
            return 1

    try:
        results = execute_setup_plan(evaluation.plan, runner=command_runner)
    except KeyboardInterrupt:
        print("Setup interrupted; local provider readiness is incomplete.")
        return 1
    try:
        refreshed_state = detect_local_installation_state(
            settings,
            runner=command_runner,
            command_identity=command_identity,
            hardware=hardware,
        )
    except KeyboardInterrupt:
        print("Setup interrupted; local provider readiness is incomplete.")
        return 1
    refreshed_evaluation = evaluate_setup(profile, refreshed_state)
    final_ready = refreshed_evaluation.ready
    print(
        format_setup_report(
            refreshed_evaluation,
            action_results=results,
            final_ready=final_ready,
        ),
        end="",
    )
    if final_ready:
        accepted_state = build_runtime_profile_state(refreshed_evaluation)
        state_path = persist_runtime_profile_state(accepted_state, paths=settings.paths)
        print(f"Accepted runtime profile state: {state_path}")
    return 0 if final_ready else 1


def _readiness_item_failed(
    readiness: Sequence[ReadinessItem],
    readiness_id: str,
) -> bool:
    return any(item.id == readiness_id and not item.ready for item in readiness)


def _requires_runtime_profile(
    transcription_provider_factory: ProviderFactory,
    diarization_provider_factory: ProviderFactory,
    semantic_coherence_provider_factory: ProviderFactory,
) -> bool:
    return production_runtime_required(
        transcription_provider_factory=transcription_provider_factory,
        diarization_provider_factory=diarization_provider_factory,
        semantic_coherence_provider_factory=semantic_coherence_provider_factory,
    )


def _run_summarize_command(
    *,
    session_path: Path,
    manual_notes: str | None,
    language: str | None,
    settings: AppSettings,
    setup_runner: CommandRunner | None = None,
    setup_command_identity: CommandIdentity | None = None,
    setup_hardware: HardwareFacts | None = None,
    allow_runtime_fallback: bool = False,
) -> int:
    session_dir, session_record = _resolve_session(session_path)
    session_id = _extract_session_id(session_record)
    summary_language = language or _session_language_or_default(
        session_record,
        settings.summary.language,
    )
    store = FileSystemArtifactStore(session_id=session_id, session_dir=session_dir)

    merged_transcript_record = _resolve_latest_merged_transcript_record(store=store)
    if merged_transcript_record is None:
        raise SummarizationError("No merged transcript artifact found for this session.")

    resolved_overlay = resolve_active_correction_overlay(
        store=store,
        source_transcript_record=merged_transcript_record,
    )
    if resolved_overlay is None:
        raise SummarizationError(
            "No completed correction overlay found."
            " Run correction and complete it before summarization."
        )
    manual_notes_record = ingest_manual_notes_file(
        store=store,
        notes_path=manual_notes,
    )
    runtime_selection = select_runtime_profile(
        settings=settings,
        runner=setup_runner,
        command_identity=setup_command_identity,
        hardware=setup_hardware,
        allow_runtime_fallback=allow_runtime_fallback,
        required_stages=SUMMARIZATION_RUNTIME_STAGES,
    )
    run_id = f"summarize-{uuid4()}"
    store_runtime_profile_provenance(
        store=store,
        run_id=run_id,
        attempt=1,
        selection=runtime_selection,
    )

    result = run_summarization_stage(
        store=store,
        ledger=ProcessingStageLedger(
            store=store,
            session_id=session_id,
            run_id=run_id,
        ),
        merged_transcript_record=merged_transcript_record,
        correction_overlay_record=resolved_overlay[0],
        manual_notes_record=manual_notes_record,
        provider=runtime_summarization_provider(
            settings=settings,
            runtime_state=runtime_selection.actual,
        ),
        policy=SummarizationPolicy(language=summary_language),
    )

    print(f"Summary written to: {result.summary_record.path}")
    return 0


def _run_export_command(
    *,
    session_path: Path,
    summary_artifact_id: str | None,
) -> int:
    verified = load_verified_session_bundle(session_path)
    session_record = dict(verified.record)
    session_id = _extract_session_id(session_record)

    export_result = export_summary_markdown(
        store=FileSystemArtifactStore(
            session_id=session_id,
            session_dir=verified.session_dir,
        ),
        summary_record_id=summary_artifact_id,
    )

    print(f"Export written to: {export_result.export_record.path}")
    print(f"Export contract: {export_result.export_record.metadata.get('contract', '(unknown)')}")
    print(
        f"Summary artifact: {export_result.summary_record.path} ({export_result.summary_record.id})"
    )
    provenance = {
        key: value
        for key, value in export_result.export_record.metadata.items()
        if key
        in {
            "summary_artifact_id",
            "summary_created_at",
            "merged_transcript_artifact_id",
            "manual_notes_artifact_id",
            "correction_overlay_artifact_id",
        }
    }
    print(f"Export provenance: {json.dumps(provenance, sort_keys=True)}")
    return 0


def _run_inspect_command(
    *,
    session_path: Path,
) -> int:
    report = verify_session_bundle(session_path)
    print(f"Integrity report for {session_path}: {'OK' if report.ok else 'FAILED'}")
    print(json.dumps(report.to_dict(), sort_keys=True, indent=2))
    return 0 if report.ok else 1


def _run_processing_command(
    *,
    session_path: Path,
    run_id: str,
    attempt: int,
    now: datetime | None,
    transcription_provider_factory: ProviderFactory,
    diarization_provider_factory: ProviderFactory,
    semantic_coherence_provider_factory: ProviderFactory,
    settings: AppSettings | None = None,
    setup_runner: CommandRunner | None = None,
    setup_command_identity: CommandIdentity | None = None,
    setup_hardware: HardwareFacts | None = None,
    allow_runtime_fallback: bool = False,
) -> int:
    runtime_selection: RuntimeProfileSelection | None = None
    session_dir, session_record = _resolve_session(session_path)
    session_id = _extract_session_id(session_record)
    profile = get_capture_profile(_required_str(session_record, "profile"))
    session_language = _session_language_or_default(session_record, "")
    _validate_processing_profile_support(profile)
    store = FileSystemArtifactStore(session_id=session_id, session_dir=session_dir)
    source_inputs = _prepare_source_audio_inputs(
        session_dir=session_dir,
        profile=profile,
        session_record=session_record,
        store=store,
    )

    if _requires_runtime_profile(
        transcription_provider_factory,
        diarization_provider_factory,
        semantic_coherence_provider_factory,
    ):
        effective_settings = settings or AppSettings.from_env()
        runtime_selection = select_runtime_profile(
            settings=effective_settings,
            runner=setup_runner,
            command_identity=setup_command_identity,
            hardware=setup_hardware,
            allow_runtime_fallback=allow_runtime_fallback,
            required_stages=PROCESSING_RUNTIME_STAGES,
        )
        store_runtime_profile_provenance(
            store=store,
            run_id=run_id,
            attempt=attempt,
            selection=runtime_selection,
        )
        transcription_provider_factory = runtime_transcription_factory(
            transcription_provider_factory,
            runtime_selection.actual,
        )
        diarization_provider_factory = runtime_diarization_factory(
            diarization_provider_factory,
            runtime_selection.actual,
        )
        semantic_coherence_provider_factory = runtime_semantic_factory(
            semantic_coherence_provider_factory,
            settings=effective_settings,
            runtime_state=runtime_selection.actual,
        )

    pipeline_timestamp = now if now is not None else datetime.now(UTC)
    transcription_provider = _require_provider(
        transcription_provider_factory,
        TranscriptionProvider,
        "transcription",
    )
    diarization_provider = _require_provider(
        diarization_provider_factory,
        DiarizationProvider,
        "diarization",
    )
    semantic_coherence_provider = _require_provider(
        semantic_coherence_provider_factory,
        SemanticCoherenceProvider,
        "semantic_coherence",
    )

    _run_processing_stages(
        session_id=session_id,
        profile=profile,
        source_inputs=source_inputs,
        store=store,
        ledger=ProcessingStageLedger(store=store, session_id=session_id, run_id=run_id),
        attempt=attempt,
        now=pipeline_timestamp,
        transcription_provider=transcription_provider,
        diarization_provider=diarization_provider,
        semantic_coherence_provider=semantic_coherence_provider,
        session_language=session_language or None,
    )

    fallback = (
        " with explicit runtime fallback"
        if runtime_selection is not None and runtime_selection.fallback_used
        else ""
    )
    print(f"Processing completed for run '{run_id}' (attempt {attempt}){fallback}.")
    return 0


def _run_processing_stages(
    *,
    session_id: str,
    profile: CaptureProfile,
    source_inputs: dict[str, SourceAudioInput],
    store: FileSystemArtifactStore,
    ledger: ProcessingStageLedger,
    attempt: int,
    now: datetime,
    transcription_provider: TranscriptionProvider,
    diarization_provider: DiarizationProvider,
    semantic_coherence_provider: SemanticCoherenceProvider,
    session_language: str | None = None,
) -> None:
    _validate_processing_profile_support(profile)

    transcript_records: dict[str, ArtifactRecord] = {}
    for source in profile.sources:
        source_id = source.logical_id.value
        source_input = source_inputs[source_id]
        transcription_result = run_transcription_stage(
            store=store,
            ledger=ledger,
            provider=transcription_provider,
            source_artifact=source_input.artifact,
            source_id=source_id,
            audio_path=source_input.path,
            language=session_language,
            attempt=attempt,
            now=now,
        )
        transcript_records[source_id] = transcription_result.transcript_record

    diarization_records: dict[str, tuple[ArtifactRecord | None, ArtifactRecord | None]] = {}
    for source in profile.sources:
        source_id = source.logical_id.value
        source_input = source_inputs[source_id]
        context = DiarizationContext(
            capture_profile=profile.name.value,
            source_id=source_id,
            enabled=source.diarisation_policy != DiarisationPolicy.NONE,
        )
        result = run_diarization_stage(
            store=store,
            ledger=ledger,
            provider=diarization_provider,
            source_artifact=source_input.artifact,
            context=context,
            audio_path=source_input.path,
            attempt=attempt,
            now=now,
        )
        if result.skipped and not result.artifact_records:
            continue
        regular: ArtifactRecord | None = None
        exclusive: ArtifactRecord | None = None
        for artifact, artifact_record in zip(
            result.artifacts, result.artifact_records, strict=True
        ):
            if artifact.kind == "regular_timeline":
                regular = artifact_record
            elif artifact.kind == "exclusive_projection":
                exclusive = artifact_record
        if regular is not None or exclusive is not None:
            diarization_records[source_id] = (regular, exclusive)

    source_ids = tuple(source.logical_id.value for source in profile.sources)
    if source_ids == ("room_mic",):
        room_regular: ArtifactRecord | None = None
        room_exclusive: ArtifactRecord | None = None
        room_records = diarization_records.get("room_mic")
        if room_records is not None:
            room_regular, room_exclusive = room_records
        merge_result = run_room_mic_merge_timing_stage(
            store=store,
            ledger=ledger,
            room_transcript_record=transcript_records["room_mic"],
            room_regular_diarization_record=room_regular,
            room_exclusive_diarization_record=room_exclusive,
            attempt=attempt,
            now=now,
        )
    else:
        local_source_id = source_ids[0]
        remote_source_id = source_ids[1]
        remote_regular: ArtifactRecord | None = None
        remote_exclusive: ArtifactRecord | None = None
        remote_records = diarization_records.get(remote_source_id)
        if remote_records is not None:
            remote_regular, remote_exclusive = remote_records

        merge_result = run_merge_timing_stage(
            store=store,
            ledger=ledger,
            local_transcript_record=transcript_records[local_source_id],
            remote_transcript_record=transcript_records[remote_source_id],
            remote_regular_diarization_record=remote_regular,
            remote_exclusive_diarization_record=remote_exclusive,
            attempt=attempt,
            now=now,
        )

    run_confidence_flagging_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merge_result.merged_transcript_record,
        attempt=attempt,
        now=now,
    )

    run_semantic_coherence_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merge_result.merged_transcript_record,
        provider=semantic_coherence_provider,
        attempt=attempt,
        now=now,
    )


def _validate_processing_profile_support(profile: CaptureProfile) -> None:
    source_ids = tuple(source.logical_id.value for source in profile.sources)
    if source_ids not in {("local_mic", "system_audio"), ("room_mic",)}:
        raise ValueError(
            "Processing currently supports online_dual_stream and in_person_single_mic profiles."
        )


def _prepare_source_audio_inputs(
    *,
    session_dir: Path,
    profile: CaptureProfile,
    session_record: dict[str, object],
    store: FileSystemArtifactStore,
) -> dict[str, SourceAudioInput]:
    required_sources = tuple(source.logical_id.value for source in profile.sources)
    capture_segments = _collect_capture_segments(session_record, required_sources)

    inputs: dict[str, SourceAudioInput] = {}
    for source_id in required_sources:
        segment_inputs: list[CaptureSegmentInput] = []
        for raw_path, index, expected_sha in capture_segments[source_id]:
            segment_path = Path(raw_path)
            if not segment_path.is_absolute():
                segment_path = session_dir / segment_path

            if not segment_path.exists():
                raise ValueError(f"Capture segment file does not exist: {segment_path}")
            if not segment_path.is_file():
                raise ValueError(f"Capture segment path is not a file: {segment_path}")

            payload = segment_path.read_bytes()
            actual_sha = hashlib.sha256(payload).hexdigest()
            if expected_sha is not None and expected_sha != actual_sha:
                raise ValueError(
                    f"Capture segment checksum mismatch for source '{source_id}' at {segment_path}."
                )

            artifact = _ensure_source_audio_artifact(
                store=store,
                source_id=source_id,
                index=index,
                path=segment_path,
                artifact_sha=actual_sha,
                payload=payload,
            )
            segment_inputs.append(
                CaptureSegmentInput(
                    source_id=source_id,
                    index=index,
                    path=segment_path,
                    artifact=artifact,
                    sha256=actual_sha,
                )
            )

        if len(segment_inputs) == 1:
            only = segment_inputs[0]
            inputs[source_id] = SourceAudioInput(
                source_id=source_id,
                index=only.index,
                path=only.path,
                artifact=only.artifact,
            )
            continue

        prep_payload, prep_metadata = _stitch_provider_prep_audio(
            source_id=source_id,
            segments=tuple(segment_inputs),
        )
        prep_sha = hashlib.sha256(prep_payload).hexdigest()
        prep_artifact = _ensure_provider_prep_audio_artifact(
            store=store,
            source_id=source_id,
            payload=prep_payload,
            artifact_sha=prep_sha,
            metadata=prep_metadata,
        )
        inputs[source_id] = SourceAudioInput(
            source_id=source_id,
            index=1,
            path=store.session_dir / prep_artifact.path,
            artifact=prep_artifact,
        )

    return inputs


def _collect_capture_segments(
    session_record: dict[str, object],
    required_sources: tuple[str, ...],
) -> dict[str, tuple[tuple[str, int, str | None], ...]]:
    capture_metadata = session_record.get("capture_metadata")
    if not isinstance(capture_metadata, dict):
        raise ValueError("Session does not include capture metadata.")

    raw_segments = capture_metadata.get("segments")
    if not isinstance(raw_segments, list):
        raise ValueError("Session capture metadata does not include segments.")

    grouped: dict[str, list[tuple[str, int, str | None]]] = {}
    for source in required_sources:
        grouped[source] = []
    for raw_segment in raw_segments:
        if not isinstance(raw_segment, dict):
            raise ValueError("Capture segment metadata must be an object.")
        source_id = _required_str(raw_segment, "source_id")
        if source_id not in grouped:
            raise ValueError(
                f"Session contains an unexpected capture source '{source_id}' for processing."
            )
        index = _required_int(raw_segment, "index")
        path = _required_str(raw_segment, "path")
        expected_sha = raw_segment.get("sha256")
        if expected_sha is not None and not isinstance(expected_sha, str):
            raise ValueError("Capture segment sha256 must be a string when provided.")
        grouped[source_id].append((path, index, expected_sha))

    selected: dict[str, tuple[tuple[str, int, str | None], ...]] = {}
    for source_id in required_sources:
        segments = grouped[source_id]
        if not segments:
            raise ValueError(f"Session is missing a capture segment for source '{source_id}'.")
        ordered = tuple(sorted(segments, key=lambda segment: segment[1]))
        indexes = [index for _, index, _ in ordered]
        if len(set(indexes)) != len(indexes):
            raise ValueError(f"Session has duplicate capture segment indexes for '{source_id}'.")
        expected_indexes = list(range(1, len(indexes) + 1))
        if indexes != expected_indexes:
            raise ValueError(
                f"Session capture segments for source '{source_id}' must be contiguous "
                "starting at 1."
            )
        selected[source_id] = ordered

    return selected


def _ensure_source_audio_artifact(
    *,
    store: FileSystemArtifactStore,
    source_id: str,
    index: int,
    path: Path,
    artifact_sha: str,
    payload: bytes,
) -> ArtifactRecord:
    normalized_path = str(path.resolve(strict=True))
    metadata_path_key = "source_path"
    metadata_index_key = "segment_index"
    metadata_source_key = "source_id"
    metadata_sha_key = "source_sha256"

    for existing in store.list(artifact_type="source_audio"):
        metadata = existing.metadata
        if (
            metadata.get(metadata_source_key) == source_id
            and metadata.get(metadata_index_key) == index
        ):
            if metadata.get(metadata_sha_key) != artifact_sha:
                raise ValueError(
                    f"Source artifact collision for {source_id} segment {index}: "
                    "checksum has changed since prior processing attempt."
                )
            verification = store.verify(existing.id)
            if not verification.ok or verification.expected_sha256 != artifact_sha:
                artifact_path = store.session_dir / existing.path
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                artifact_path.write_bytes(payload)
                verification = store.verify(existing.id)
                if not verification.ok or verification.expected_sha256 != artifact_sha:
                    raise ValueError(
                        f"Source artifact verification failed for {source_id} segment {index}."
                    )
            return existing

    return store.store(
        artifact_type="source_audio",
        name=f"source_audio/{source_id}_{index:06d}.wav",
        data=payload,
        status="sealed",
        metadata={
            metadata_source_key: source_id,
            metadata_index_key: index,
            metadata_path_key: normalized_path,
            metadata_sha_key: artifact_sha,
        },
    )


def _stitch_provider_prep_audio(
    *,
    source_id: str,
    segments: tuple[CaptureSegmentInput, ...],
) -> tuple[bytes, dict[str, object]]:
    if len(segments) < 2:
        raise ValueError("Provider-prep audio requires at least two source segments.")

    params: tuple[int, int, int] | None = None
    frame_chunks: list[bytes] = []
    frame_count = 0
    for segment in segments:
        with wave.open(str(segment.path), "rb") as wav_file:
            if wav_file.getcomptype() != "NONE":
                raise ValueError(
                    f"Capture segment for source '{source_id}' must be uncompressed PCM WAV."
                )
            segment_params = (
                wav_file.getframerate(),
                wav_file.getnchannels(),
                wav_file.getsampwidth(),
            )
            if params is None:
                params = segment_params
            elif segment_params != params:
                raise ValueError(
                    f"Capture segments for source '{source_id}' have incompatible WAV format."
                )
            frames = wav_file.readframes(wav_file.getnframes())
            frame_chunks.append(frames)
            frame_count += wav_file.getnframes()

    if params is None:
        raise ValueError(f"Session is missing provider-prep segments for source '{source_id}'.")
    sample_rate, channels, sample_width = params
    output = BytesIO()
    with wave.open(output, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(sample_width)
        out.setframerate(sample_rate)
        out.writeframes(b"".join(frame_chunks))
    payload = output.getvalue()
    metadata: dict[str, object] = {
        "source_id": source_id,
        "derived_from_segments": [
            {
                "source_id": segment.source_id,
                "index": segment.index,
                "path": str(segment.path),
                "artifact_id": segment.artifact.id,
                "sha256": segment.sha256,
            }
            for segment in segments
        ],
        "transformations": ["lossless_wav_stitch"],
        "provider_requirement": "single_audio_path",
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_format": f"pcm_s{sample_width * 8}le",
        "duration_seconds": frame_count / sample_rate,
    }
    metadata["sha256"] = hashlib.sha256(payload).hexdigest()
    return payload, metadata


def _ensure_provider_prep_audio_artifact(
    *,
    store: FileSystemArtifactStore,
    source_id: str,
    payload: bytes,
    artifact_sha: str,
    metadata: dict[str, object],
) -> ArtifactRecord:
    for existing in store.list(artifact_type="provider_prep_audio"):
        if existing.metadata.get("source_id") != source_id:
            continue
        if existing.sha256 != artifact_sha:
            raise ValueError(
                f"Provider-prep artifact collision for source '{source_id}': "
                "prepared audio bytes changed since a prior processing attempt."
            )
        verification = store.verify(existing.id)
        if not verification.ok or verification.expected_sha256 != artifact_sha:
            artifact_path = store.session_dir / existing.path
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_bytes(payload)
            verification = store.verify(existing.id)
            if not verification.ok or verification.expected_sha256 != artifact_sha:
                raise ValueError(
                    f"Provider-prep artifact verification failed for source '{source_id}'."
                )
        return existing

    return store.store(
        artifact_type="provider_prep_audio",
        name=f"provider-prep/{source_id}.wav",
        data=payload,
        status="sealed",
        metadata=metadata,
    )


def _latest_processing_state(
    store: FileSystemArtifactStore,
    session_id: str,
    *,
    allowed_statuses: frozenset[str],
    require_retryable_error: bool | None = None,
) -> ProcessingStageState | None:
    matches: list[tuple[int, ProcessingStageState]] = []
    for state_record in store.list(artifact_type=PROCESSING_STATE_ARTIFACT_TYPE):
        payload = json.loads(store.retrieve(state_record.id))
        state = ProcessingStageState.from_dict(payload)
        if (
            state.session_id != session_id
            or state.status not in allowed_statuses
            or state.stage not in PROCESSING_PIPELINE_STAGES
        ):
            continue

        if state.status == "failed":
            if state.error is None:
                continue
            if require_retryable_error is True and not state.error.retryable:
                continue
            if require_retryable_error is False and state.error.retryable:
                continue

        matches.append((_processing_state_order(state_record), state))

    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def _processing_state_order(record: ArtifactRecord) -> int:
    prefix = Path(record.path).stem.split("-", 1)[0]
    try:
        return int(prefix)
    except ValueError as exc:
        raise ProcessingStateError(
            "Processing state artifact name must start with sequence."
        ) from exc


def _latest_retryable_processing_state(
    store: FileSystemArtifactStore,
    session_id: str,
) -> ProcessingStageState | None:
    return _latest_processing_state(
        store=store,
        session_id=session_id,
        allowed_statuses=frozenset({"failed"}),
        require_retryable_error=True,
    )


def _latest_resumable_processing_state(
    store: FileSystemArtifactStore,
    session_id: str,
) -> ProcessingStageState | None:
    latest = _latest_processing_state(
        store=store,
        session_id=session_id,
        allowed_statuses=frozenset({"running", "completed", "failed", "skipped"}),
    )
    if latest is None or latest.status == "running":
        return latest
    if latest.status in {"completed", "skipped"}:
        return None if _is_final_processing_stage(latest) else latest
    if latest.error is not None and latest.error.retryable:
        return latest
    return None


def _is_final_processing_stage(state: ProcessingStageState) -> bool:
    return state.stage == SEMANTIC_COHERENCE_STAGE


def _latest_non_retryable_processing_failure(
    store: FileSystemArtifactStore,
    session_id: str,
) -> ProcessingStageState | None:
    latest = _latest_processing_state(
        store=store,
        session_id=session_id,
        allowed_statuses=frozenset({"running", "completed", "failed", "skipped"}),
    )
    if latest is None or latest.status != "failed":
        return None
    if latest.error is not None and not latest.error.retryable:
        return latest
    return None


def _resolve_latest_merged_transcript_record(
    *,
    store: ArtifactStore,
) -> ArtifactRecord | None:
    best: tuple[tuple[datetime, str, str], ArtifactRecord] | None = None
    for record in store.list(artifact_type=MERGED_TRANSCRIPT_ARTIFACT_TYPE, status="sealed"):
        key = _merged_transcript_timestamp_key(store=store, record=record)
        if key is None:
            continue
        item = (key, record)
        if best is None or item > best:
            best = item
    return None if best is None else best[1]


def _merged_transcript_timestamp_key(
    *,
    store: ArtifactStore,
    record: ArtifactRecord,
) -> tuple[datetime, str, str] | None:
    if not store.verify(record.id).ok:
        return None
    try:
        merged = MergedTranscriptArtifact.from_dict(
            json.loads(store.retrieve(record.id).decode("utf-8"))
        )
    except (json.JSONDecodeError, UnicodeDecodeError, MergeTimingContractError):
        return None
    created_at = _parse_iso_timestamp(merged.created_at)
    if created_at is None:
        return None
    return (created_at, record.path, record.id)


def _parse_iso_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _resolve_session(session_path: Path) -> tuple[Path, dict[str, object]]:
    if session_path.is_dir():
        session_record_path = session_path / SESSION_RECORD_FILENAME
        resolved_session_dir = session_path
    elif session_path.is_file() and session_path.name == SESSION_RECORD_FILENAME:
        session_record_path = session_path
        resolved_session_dir = session_path.parent
    else:
        raise ValueError(
            "Session path must be a session directory or an existing session.json path."
        )

    try:
        record = read_session_record(session_record_path)
    except Exception as exc:
        raise ValueError(f"Cannot read session record at {session_record_path}.") from exc

    return resolved_session_dir, record


def _extract_session_id(session_record: dict[str, object]) -> str:
    return _required_str(session_record, "id")


def _session_language_or_default(
    session_record: dict[str, object],
    default_language: str,
) -> str:
    language = session_record.get("language")
    if isinstance(language, str) and language:
        return language
    return default_language


def _required_str(payload: dict[str, object] | object, key: str) -> str:
    value = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value:
        raise ValueError(f"Session field '{key}' is required and must be a non-empty string.")
    return value


def _required_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"Session field '{key}' must be a positive integer.")
    return value


def _provider_implements(provider: Any, *required_attrs: str) -> bool:
    for attr in required_attrs:
        if not hasattr(provider, attr):
            return False
        if attr in {"transcribe", "diarize", "check"} and not callable(getattr(provider, attr)):
            return False
    return True


def _require_provider(
    provider_factory: ProviderFactory,
    provider_type: type[Any],
    provider_name: str,
) -> Any:
    try:
        provider = provider_factory()
    except TypeError as exc:
        raise ProviderValidationError(
            f"{provider_name} provider must implement {provider_type.__name__}."
        ) from exc
    required: tuple[str, ...] = ()
    if provider_type is TranscriptionProvider:
        required = ("name", "transcribe")
    elif provider_type is DiarizationProvider:
        required = ("name", "config_id", "diarize")
    elif provider_type is SemanticCoherenceProvider:
        required = ("metadata", "check")

    if required:
        if not _provider_implements(provider, *required):
            raise ProviderValidationError(
                f"{provider_name} provider must implement {provider_type.__name__}."
            )
        if provider_type is SemanticCoherenceProvider:
            metadata = provider.metadata
            config_id = getattr(metadata, "config_id", None)
            if not isinstance(config_id, str) or not config_id:
                raise ProviderValidationError(
                    f"{provider_name} provider must expose semantic metadata config_id."
                )
            if not callable(getattr(metadata, "to_dict", None)):
                raise ProviderValidationError(
                    f"{provider_name} provider must expose semantic metadata to_dict."
                )
        return provider

    try:
        valid = isinstance(provider, provider_type)
    except TypeError:
        valid = False
    if not valid:
        raise ProviderValidationError(
            f"{provider_name} provider must implement {provider_type.__name__}."
        )
    return provider
