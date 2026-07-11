"""CLI adapter for the mena PoC."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from shlex import quote
from typing import Any, TypeVar, cast
from uuid import uuid4

from mena import export_workflow, processing_orchestration, summarize_workflow
from mena.artifacts import (
    ArtifactRecord,
    ArtifactStore,
    ArtifactStoreError,
    FileSystemArtifactStore,
)
from mena.correction_overlay import (
    CorrectionOverlayContractError,
)
from mena.correction_workflow import (
    CorrectionWorkflowError,
    PlaybackPlayer,
    run_correction_workflow,
)
from mena.diarization import (
    DiarizationError,
    run_diarization_stage,
)
from mena.discovery import (
    AudioDiscovery,
    discover_audio_routes,
    format_discovery,
)
from mena.export import (
    SummaryMarkdownExportError,
)
from mena.fields import optional_str, required_int, required_str
from mena.interaction import (
    ChoiceOption,
    ChoiceRequest,
    ConfirmationRequest,
    InteractionPort,
    TextRequest,
    WorkflowNotice,
)
from mena.manual_notes import ManualNotesIngestionError
from mena.merge_timing import (
    MERGED_TRANSCRIPT_ARTIFACT_TYPE,
    MergedTranscriptArtifact,
    MergeTimingContractError,
    MergeTimingError,
    run_merge_timing_stage,
    run_room_mic_merge_timing_stage,
)
from mena.processing import (
    PROCESSING_STATE_ARTIFACT_TYPE,
    ProcessingStageState,
    ProcessingStateError,
    latest_non_retryable_processing_failure,
    latest_resumable_processing_state,
)
from mena.profiles import (
    CaptureProfileSelection,
    capture_profile_choices,
    parse_capture_profile,
)
from mena.protocol import (
    StartCapturePayload,
    WorkerProcess,
)
from mena.provider_validation import ProviderValidationError
from mena.recording_control import (
    RecordCommandInput,
    format_record_diagnostics,
    run_recording_control_workflow,
)
from mena.review_flagging import (
    ReviewFlaggingError,
    run_confidence_flagging_stage,
)
from mena.runtime_profile import (
    RuntimeFallbackUnavailable,
    bind_summarization_runtime,
    select_summarization_runtime_profile,
    store_runtime_profile_provenance,
)
from mena.runtime_stewardship import (
    RuntimeReleaseResult,
    release_terminal_local_llm,
    store_runtime_stewardship_provenance,
)
from mena.semantic_coherence import (
    SEMANTIC_COHERENCE_STAGE,
    SemanticCoherenceError,
    run_semantic_coherence_stage,
)
from mena.session_integrity import (
    SessionIntegrityError,
    verify_session_bundle,
)
from mena.sessions import (
    ProcessableSessionCandidate,
    processable_session_candidates,
    read_session_record,
    resolve_session,
    session_id,
)
from mena.settings import DATA_ROOT_ENV_VAR, AppSettings, SettingsError, validate_summary_language
from mena.setup import (
    CommandIdentity,
    CommandRunner,
    HardwareFacts,
    SetupEvaluation,
)
from mena.setup_detection import DefaultSetupDetector
from mena.setup_workflow import (
    SetupMessageEvent,
    SetupReportEvent,
    SetupWorkflowOptions,
    run_setup_workflow,
)
from mena.summarization import (
    SummarizationError,
    run_summarization_stage,
)
from mena.timestamps import parse_utc_timestamp
from mena.transcription import (
    TranscriptionError,
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
ProviderFactory = Callable[[], Any]
ChoiceT = TypeVar("ChoiceT")


class ConsoleRecordInteraction:
    """Console adapter for record workflow interaction topics."""

    def __init__(self, input_fn: Callable[[str], str] | None) -> None:
        self._input_fn = input_fn

    def choose(self, request: ChoiceRequest) -> ChoiceOption:
        reader = _record_input(self._input_fn)
        if reader is None:
            if request.topic == "record.capture_route.playback_stream":
                raise ValueError(
                    "Playback stream confirmation is required; pass --playback-stream."
                )
            role, flag_name, _header, _label = _record_choice_prompt_metadata(request.topic)
            raise ValueError(f"Multiple {role} routes were discovered; pass {flag_name}.")
        _role, _flag_name, header, label = _record_choice_prompt_metadata(request.topic)
        choices = request.options
        default_index = 1
        if request.default_option_id is not None:
            for index, option in enumerate(choices, start=1):
                if option.option_id == request.default_option_id:
                    default_index = index
                    break
        return _prompt_choice(
            header=header,
            label=label,
            choices=choices,
            formatter=lambda option: option.label,
            input_fn=reader,
            default_index=default_index,
        )

    def confirm(self, request: ConfirmationRequest) -> bool:
        raise ValueError(f"Unsupported record confirmation topic '{request.topic}'.")

    def request_text(self, request: TextRequest) -> str:
        reader = _record_input(self._input_fn)
        if reader is None:
            raise RuntimeError("Recording requires an interactive terminal to stop.")
        if request.topic == "record.stop_command":
            return reader("record> ")
        raise ValueError(f"Unsupported record text topic '{request.topic}'.")

    def notify(self, notice: WorkflowNotice) -> None:
        topic = notice.topic
        context = notice.context
        if topic == "record.session_created":
            print(f"Created session {context['session_id']}")
        elif topic == "record.session_record":
            print(f"Session record: {context['record_path']}")
        elif topic == "record.discovery":
            print(format_discovery(cast(AudioDiscovery, context["discovery"])))
        elif topic == "record.capture_not_started.discovery_unavailable":
            print("Capture not started: audio discovery is unavailable.")
        elif topic == "record.capture_not_started.error":
            print(f"Capture not started: {context['error']}")
        elif topic == "record.capture_not_started.noninteractive_stop":
            print("Capture not started: recording requires an interactive terminal to stop.")
        elif topic == "record.setup_cancelled":
            print("Recording setup cancelled.")
        elif topic == "record.diagnostics":
            print(format_record_diagnostics(cast(StartCapturePayload, context["start_payload"])))
        elif topic == "record.consent_notice":
            print(
                "Recording notice: you are responsible for obtaining consent "
                "from meeting participants."
            )
        elif topic == "record.stop_prompt_started":
            print("Recording. Press q to stop.")
        elif topic == "record.stop_prompt_continues":
            print("Recording continues. Type q to stop.")
        elif topic == "record.segment_flushed":
            print(f"Sealed {context['source_id']} segment {context['index']}.")
        elif topic == "record.capture_status":
            print(
                "Recording: "
                f"{int(cast(float, context['elapsed_seconds']))}s elapsed, "
                f"{context['sealed_segment_count']} sealed segment(s)."
            )
        elif topic == "record.capture_route_event":
            print(_format_capture_route_event(context))
        elif topic == "record.worker_teardown_interrupted":
            print("Worker teardown interrupted after capture finalization.")
        elif topic == "record.stop_interrupted":
            print("Recording stop requested; finalizing capture.")
        elif topic == "record.capture_failed":
            print(f"Capture failed: {context['error']}")
        elif topic == "record.capture_finalized":
            print("Capture finalized")
        elif topic == "record.process_handoff":
            print("Recording saved and ready to process.")
            print(f"Next command: {_format_process_handoff_command(context.get('data_root'))}")
            print(f"Session record: {context['record_path']}")
            summary = _format_capture_route_handoff_summary(context)
            if summary is not None:
                print(summary)
        elif topic == "record.finalized_status_error":
            print(f"Capture finalized as {context['status']}: {context['error']}")
        elif topic == "record.finalized_status":
            print(f"Capture finalized as {context['status']}.")
        else:
            raise ValueError(f"Unsupported record notice topic '{topic}'.")


def _format_capture_route_event(context: Mapping[str, object]) -> str:
    event = context.get("event")
    message = cast(str, context.get("message", ""))
    if event == "recovered":
        return f"Capture recovered: {message}"
    if not isinstance(message, str) or message.strip() == "":
        message = "capture route changed."
    source_id = context.get("source_id")
    code = context.get("code")
    selected_sources = context.get("selected_sources", ())
    if not isinstance(selected_sources, (list, tuple)):
        selected_sources = ()
    continued_capture_summary = _format_continued_capture_summary(selected_sources)
    playback_route_failed = (
        event == "degraded"
        and source_id == "system_audio"
        and code == "dedicated_route_runtime_failed"
    )
    if playback_route_failed:
        return (
            f"Selected playback stream ended during capture; {continued_capture_summary}. {message}"
        )
    if source_id == "system_audio":
        return f"Capture degraded (system audio): {message}"
    return f"Capture degraded: {message}"


def _format_continued_capture_summary(selected_sources: Sequence[object]) -> str:
    selected_source_ids = tuple(source for source in selected_sources if isinstance(source, str))
    continuing_sources = tuple(source for source in selected_source_ids if source != "system_audio")
    if "local_mic" in continuing_sources:
        return "local microphone capture may have continued"
    if continuing_sources:
        return "other selected source capture may have continued"
    return "no other selected source was active"


def _format_capture_route_handoff_summary(context: Mapping[str, object]) -> str | None:
    record_path_value = context.get("record_path")
    if not isinstance(record_path_value, (str, Path)):
        return None
    record_path = Path(record_path_value)
    try:
        record = read_session_record(record_path)
    except (OSError, ValueError):
        return None
    continued_capture_summary = _format_handoff_continued_capture_summary(record)
    degradations = record.get("degradations")
    if not isinstance(degradations, list):
        return None
    for event in degradations:
        if not isinstance(event, Mapping):
            continue
        if (
            event.get("source_id") == "system_audio"
            and event.get("code") == "dedicated_route_runtime_failed"
        ):
            return (
                "Finalization note: selected playback stream ended during recording; "
                f"{continued_capture_summary}."
            )
    return None


def _format_handoff_continued_capture_summary(record: Mapping[str, object]) -> str:
    selected_sources = _selected_capture_sources(record)
    continuing_sources = tuple(source for source in selected_sources if source != "system_audio")
    recorded_sources = _recorded_capture_sources(record)
    recorded_continuing_sources = tuple(
        source for source in continuing_sources if source in recorded_sources
    )
    if "local_mic" in recorded_continuing_sources:
        return "local microphone segments were recorded"
    if recorded_continuing_sources:
        return "other selected source segments were recorded"
    if "local_mic" in continuing_sources:
        return (
            "local microphone capture was selected, but no local microphone segments were recorded"
        )
    if continuing_sources:
        return "other selected source capture was selected, but no matching segments were recorded"
    return "no other selected source was active"


def _selected_capture_sources(record: Mapping[str, object]) -> tuple[str, ...]:
    capture_plan = record.get("capture_plan")
    if not isinstance(capture_plan, Mapping):
        return ()
    selected = cast(object, capture_plan.get("selected_sources"))
    if not isinstance(selected, (list, tuple)):
        return ()
    return tuple(item for item in selected if isinstance(item, str))


def _recorded_capture_sources(record: Mapping[str, object]) -> frozenset[str]:
    capture_metadata = record.get("capture_metadata")
    if not isinstance(capture_metadata, Mapping):
        return frozenset()
    sources = capture_metadata.get("sources")
    if not isinstance(sources, list):
        return frozenset()
    recorded_sources: set[str] = set()
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        source_id = source.get("source_id")
        segment_count = source.get("segment_count")
        if isinstance(source_id, str) and isinstance(segment_count, int) and segment_count > 0:
            recorded_sources.add(source_id)
    return frozenset(recorded_sources)


def _record_choice_prompt_metadata(topic: str) -> tuple[str, str, str, str]:
    if topic == "record.capture_route.microphone":
        return ("microphone", "--mic", "Microphone routes:", "microphone routes")
    if topic == "record.capture_route.playback_stream":
        return ("playback stream", "--playback-stream", "Playback streams:", "playback streams")
    if topic == "record.capture_route.output_sink":
        return (
            "meeting audio output",
            "--output-sink",
            "Where do you hear the meeting?",
            "meeting audio output",
        )
    raise ValueError(f"Unsupported record choice topic '{topic}'.")


class ConsoleCorrectionInteraction:
    """Console adapter for correction workflow interaction topics."""

    def __init__(self, input_fn: Callable[[str], str] | None) -> None:
        self._input_fn = input if input_fn is None else input_fn

    def choose(self, request: ChoiceRequest) -> ChoiceOption:
        if request.topic != "correction.action":
            raise ValueError(f"Unsupported correction choice topic '{request.topic}'.")
        options = {option.option_id: option for option in request.options}
        while True:
            raw = (
                self._input_fn(
                    "Action [p]lay, [s]kip, [r]eassign speaker, [t]ext edit, or [o]verlap: "
                )
                .strip()
                .lower()
            )
            option_id = _correction_action_option_id(raw)
            if option_id is not None and option_id in options:
                return options[option_id]
            print("Invalid action. Use [p]lay, [s]kip, [r]eassign, [t]ext, or [o]verlap.")

    def confirm(self, request: ConfirmationRequest) -> bool:
        if request.topic != "correction.overlap_mark":
            raise ValueError(f"Unsupported correction confirmation topic '{request.topic}'.")
        while True:
            raw = self._input_fn("Mark overlap? [y/N]: ").strip().lower()
            if raw in {"", "n", "no"}:
                return False
            if raw in {"y", "yes"}:
                return True
            print("Please answer y or n.")

    def request_text(self, request: TextRequest) -> str:
        if request.topic == "correction.speaker_assignment":
            return self._input_fn("Choose existing speaker number or enter a new speaker label: ")
        if request.topic == "correction.text_edit":
            return self._input_fn("Corrected text: ")
        raise ValueError(f"Unsupported correction text topic '{request.topic}'.")

    def notify(self, notice: WorkflowNotice) -> None:
        topic = notice.topic
        context = notice.context
        if topic == "correction.speaker_assignment.invalid_number":
            print("Choose a valid number from the list.")
        elif topic == "correction.speaker_assignment.empty":
            print("Enter a speaker id or a label.")
        elif topic == "correction.playback_failed":
            print(f"Playback failed: {context['error']}")
        elif topic == "correction.playback_completed":
            print(
                "Playback completed for "
                f"{context['source_id']} "
                f"{cast(float, context['start_seconds']):.2f}-"
                f"{cast(float, context['end_seconds']):.2f}s."
            )
        elif topic == "correction.completed":
            print(
                "Correction completed. "
                f"reviewed={context['reviewed']}, "
                f"skipped={context['skipped']}, "
                f"corrected={context['corrected']}."
            )
        else:
            raise ValueError(f"Unsupported correction notice topic '{topic}'.")


class ConsoleProcessingInteraction:
    """Console adapter for processing workflow interaction topics."""

    def __init__(self, input_fn: Callable[[str], str]) -> None:
        self._input_fn = input_fn

    def choose(self, request: ChoiceRequest) -> ChoiceOption:
        raise ValueError(f"Unsupported processing choice topic '{request.topic}'.")

    def confirm(self, request: ConfirmationRequest) -> bool:
        if request.topic != "runtime_fallback.one_run":
            raise ValueError(f"Unsupported processing confirmation topic '{request.topic}'.")
        accepted_failure = str(request.context["accepted_failure"])
        fallback_evaluation = cast(SetupEvaluation, request.context["fallback_evaluation"])
        print(f"Accepted runtime profile is unavailable: {accepted_failure}")
        print(
            "One-run local runtime fallback is ready: "
            f"{fallback_evaluation.profile.id} using "
            f"{fallback_evaluation.selected_asr_profile.checkpoint}/"
            f"{fallback_evaluation.selected_asr_profile.device}."
        )
        answer = self._input_fn("Use local runtime fallback for this run? [y/N] ")
        return answer.strip().lower() in {"y", "yes"}

    def request_text(self, request: TextRequest) -> str:
        raise ValueError(f"Unsupported processing text topic '{request.topic}'.")

    def notify(self, notice: WorkflowNotice) -> None:
        raise ValueError(f"Unsupported processing notice topic '{notice.topic}'.")


class ConsoleSummarizeInteraction:
    """Console adapter for summarize workflow interaction topics."""

    def __init__(self, input_fn: Callable[[str], str] | None) -> None:
        self._input_fn = input_fn

    def choose(self, request: ChoiceRequest) -> ChoiceOption:
        if request.topic != "summarize.session":
            raise ValueError(f"Unsupported summarize choice topic '{request.topic}'.")
        if request.default_option_id is None:
            raise ValueError("Summarize session choice requires a recommended default.")

        reader = _record_input(self._input_fn)
        default = request.option(request.default_option_id)
        if reader is None:
            print(
                "Multiple ready sessions found for summarization."
                f" Using newest session by default: {_format_summarize_choice(default)}"
            )
            print("Pass the session path argument to summarize a different session.")
            return default

        return _prompt_choice(
            header="Ready session to summarize:",
            label="ready session",
            choices=request.options,
            formatter=lambda option: _format_summarize_choice(
                option,
                recommended=option.option_id == request.default_option_id,
            ),
            input_fn=reader,
            default_index=_choice_default_index(request),
        )

    def confirm(self, request: ConfirmationRequest) -> bool:
        if request.topic != "runtime_fallback.summarization_remote":
            raise ValueError(f"Unsupported summarize confirmation topic '{request.topic}'.")
        reader = _record_input(self._input_fn)
        reason = str(request.context.get("reason", "accepted provider unavailable"))
        print(f"Accepted summarization provider is unavailable: {reason}")
        disclosure = request.context.get("disclosure", ())
        if isinstance(disclosure, tuple):
            print("Mistral summarization disclosure:")
            for line in disclosure:
                print(f"  - {line}")
        if reader is None:
            print(
                "Remote Mistral fallback is not pre-authorized and this run is "
                "non-interactive; declining. Re-run setup with "
                "--authorize-mistral-summary-fallback to pre-authorize it."
            )
            return False
        answer = reader("Use Mistral summarization fallback for this run? [y/N] ")
        return answer.strip().lower() in {"y", "yes"}

    def request_text(self, request: TextRequest) -> str:
        raise ValueError(f"Unsupported summarize text topic '{request.topic}'.")

    def notify(self, notice: WorkflowNotice) -> None:
        topic = notice.topic
        context = notice.context
        if topic == "summarize.session_selected":
            print(f"Session path: {context['session_dir']}")
        elif topic == "summarize.started":
            print("Creating meeting notes...")
        elif topic == "summarize.completed":
            print(f"Notes created: {context['summary_path']}")
            print(f"Session path: {context['session_dir']}")
            print(
                "Next command: "
                f"mena export --summary-artifact-id {quote(str(context['summary_artifact_id']))} "
                f"{quote(str(context['session_dir']))}"
            )
        else:
            raise ValueError(f"Unsupported summarize notice topic '{topic}'.")


class ConsoleExportInteraction:
    """Console adapter for export workflow interaction topics."""

    def __init__(self, input_fn: Callable[[str], str] | None) -> None:
        self._input_fn = input_fn

    def choose(self, request: ChoiceRequest) -> ChoiceOption:
        raise ValueError(f"Unsupported export choice topic '{request.topic}'.")

    def confirm(self, request: ConfirmationRequest) -> bool:
        if request.topic != "export.format.markdown":
            raise ValueError(f"Unsupported export confirmation topic '{request.topic}'.")
        if self._input_fn is None:
            return True
        print("Available export format:")
        print("  1) Markdown (.md)")
        answer = self._input_fn("Export as Markdown notes? [Y/n]: ")
        return answer.strip().lower() in {"", "1", "y", "yes"}

    def request_text(self, request: TextRequest) -> str:
        raise ValueError(f"Unsupported export text topic '{request.topic}'.")

    def notify(self, notice: WorkflowNotice) -> None:
        topic = notice.topic
        context = notice.context
        if topic == "export.session_selected.latest":
            print(f"Using latest summarized session: {context['session_path']}")
        elif topic == "export.session_selected.summary_artifact":
            print(f"Using session containing requested summary artifact: {context['session_path']}")
        elif topic == "export.completed":
            print(f"Markdown notes exported to: {context['export_path']}")
        else:
            raise ValueError(f"Unsupported export notice topic '{topic}'.")


def _correction_action_option_id(raw: str) -> str | None:
    if raw in {"p", "play", "playback"}:
        return "playback"
    if raw in {"s", "skip"}:
        return "skip"
    if raw in {"r", "speaker", "reassign", "speaker_reassignment"}:
        return "speaker_reassignment"
    if raw in {"t", "text", "text_edit", "edit"}:
        return "text_edit"
    if raw in {"o", "overlap", "overlap_mark"}:
        return "overlap_mark"
    return None


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
        help="Meeting audio output route id.",
    )
    record_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Create a session and print read-only audio discovery without starting capture.",
    )
    record_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print route planning details and write worker capture diagnostics to stderr.",
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
    setup_parser.add_argument(
        "--authorize-local-fallback",
        action="store_true",
        help="Authorize automatic local runtime fallback when the fallback profile is ready.",
    )
    setup_parser.add_argument(
        "--summarization-provider",
        choices=("local", "mistral"),
        default="local",
        help=(
            "Accepted summarization primary provider. 'mistral' uses the fixed "
            "allow-listed direct Mistral model and requires the disclosure to be "
            "accepted."
        ),
    )
    setup_parser.add_argument(
        "--authorize-mistral-summary-fallback",
        action="store_true",
        help=(
            "Accept Mistral as the first ordered summarization fallback provider "
            "for a local primary; requires the disclosure to be accepted."
        ),
    )
    setup_parser.add_argument(
        "--accept-mistral-summary-disclosure",
        action="store_true",
        help=(
            "Acknowledge the Mistral summarization disclosure non-interactively. "
            "--yes alone never accepts the disclosure."
        ),
    )
    setup_parser.set_defaults(command="setup")

    for command in COMMANDS[1:]:
        if command == "setup":
            continue
        subparser = subparsers.add_parser(command)
        if command in {"correct", "process", "resume", "summarize", "export", "inspect"}:
            if command == "export":
                subparser.add_argument(
                    "session_path",
                    nargs="?",
                    help="Path to the session directory or session.json.",
                )
            elif command == "process":
                subparser.add_argument(
                    "session_path",
                    nargs="?",
                    help=(
                        "Path to the session directory or session.json. "
                        "Defaults to the latest recorded session."
                    ),
                )
            elif command == "summarize":
                subparser.add_argument(
                    "session_path",
                    nargs="?",
                    help="Path to the session directory or session.json.",
                    default=None,
                )
            else:
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
                "--format",
                choices=("markdown",),
                dest="export_format",
                default=None,
                help="Export output format.",
            )
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
    transcription_provider_factory: ProviderFactory | None = None,
    diarization_provider_factory: ProviderFactory | None = None,
    semantic_coherence_provider_factory: ProviderFactory | None = None,
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
        sessions_root = (
            data_root / "sessions" if data_root is not None else app_settings.paths.sessions_dir
        )
    except SettingsError as exc:
        print(f"Cannot load configuration: {exc}")
        return 1

    effective_semantic_coherence_provider_factory = semantic_coherence_provider_factory

    if args.command == "setup":
        return _run_setup_command(
            check_only=args.check,
            assume_yes=args.yes,
            authorize_local_fallback=args.authorize_local_fallback,
            summarization_provider=args.summarization_provider,
            mistral_summary_fallback=args.authorize_mistral_summary_fallback,
            accept_mistral_disclosure=args.accept_mistral_summary_disclosure,
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

        result = run_recording_control_workflow(
            RecordCommandInput(
                profile=profile,
                language=language,
                name=args.name,
                microphone_route_id=args.mic,
                playback_stream_route_id=args.playback_stream,
                output_sink_route_id=args.output_sink,
                dry_run=args.dry_run,
                verbose=args.verbose,
                worker_exit_timeout=args.worker_exit_timeout,
                stop_input_available=_record_input(input_fn) is not None,
            ),
            data_root=_effective_record_data_root(data_root, app_settings),
            discoverer=discoverer,
            worker_factory=worker_factory,
            interaction=ConsoleRecordInteraction(input_fn),
            now=now,
        )
        return result.exit_code

    if args.command == "correct":
        try:
            run_correction_workflow(
                session_path=Path(args.session_path),
                now=now,
                interaction=ConsoleCorrectionInteraction(input_fn),
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
            interaction = ConsoleSummarizeInteraction(input_fn)
            resolved_session_path = summarize_workflow.resolve_summarize_session_path(
                explicit_session_path=args.session_path,
                sessions_root=sessions_root,
                interaction=interaction,
            )
            return _run_summarize_command(
                session_path=resolved_session_path,
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
                interaction=interaction,
            )
        except RuntimeFallbackUnavailable as exc:
            # Recoverable: the accepted summarization provider failed and an
            # accepted fallback exists but is not authorized (or none is
            # configured). Exit code 3 mirrors the processing contract.
            print(f"Cannot summarize session: {exc}")
            return 3
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
                session_path=Path(args.session_path) if args.session_path else None,
                summary_artifact_id=args.summary_artifact_id,
                export_format=args.export_format,
                settings=_settings_with_sessions_root(app_settings, sessions_root),
                input_fn=input_fn,
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
        except (EOFError, KeyboardInterrupt):
            print("Export cancelled.")
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
                session_path = _resolve_process_session_path(
                    args.session_path,
                    app_settings,
                    input_fn=input_fn,
                )
            else:
                session_path = Path(args.session_path)
                resolved_session = resolve_session(session_path)
                session_id_value = session_id(resolved_session.record)
                store = FileSystemArtifactStore(
                    session_id=session_id_value,
                    session_dir=resolved_session.session_dir,
                )
                resumable = latest_resumable_processing_state(
                    store,
                    session_id=session_id_value,
                )
                if resumable is None:
                    blocked = latest_non_retryable_processing_failure(
                        store,
                        session_id=session_id_value,
                    )
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

            if args.command == "process":
                print(f"Processing recording: {session_path}")
            exit_code = processing_orchestration.run_processing_command(
                session_path=session_path,
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
                interaction=_resolve_runtime_fallback_interaction(input_fn),
                terminal_llm_release=_record_terminal_local_llm_release,
                transcription_stage_runner=run_transcription_stage,
                diarization_stage_runner=run_diarization_stage,
                merge_timing_stage_runner=run_merge_timing_stage,
                room_mic_merge_timing_stage_runner=run_room_mic_merge_timing_stage,
                confidence_flagging_stage_runner=run_confidence_flagging_stage,
                semantic_coherence_stage_runner=run_semantic_coherence_stage,
            )
            if args.command == "process" and exit_code == 0:
                print("Processing completed successfully.")
                if _correction_handoff_available(session_path):
                    print(f"Next command: mena correct {quote(str(session_path))}")
            return exit_code
        except RuntimeFallbackUnavailable as exc:
            # Recoverable: a GPU stage failed and a CPU fallback exists but is not
            # authorized. Distinct exit code 3 lets an automated caller re-invoke
            # with --allow-runtime-fallback instead of treating it as a hard error.
            print(f"Cannot run processing workflow: {exc}")
            return 3
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


def _prompt_choice(
    *,
    header: str,
    label: str,
    choices: tuple[ChoiceT, ...],
    formatter: Callable[[ChoiceT], str],
    input_fn: Callable[[str], str],
    default_index: int = 1,
) -> ChoiceT:
    if default_index < 1 or default_index > len(choices):
        raise ValueError(f"Invalid default {label} selection '{default_index}'.")
    print(header)
    for index, choice in enumerate(choices, start=1):
        print(f"  {index}. {formatter(choice)}")
    answer = input_fn(f"Select {label} [{default_index}]: ").strip() or str(default_index)
    if not answer.isdigit():
        raise ValueError(f"Invalid {label} selection '{answer}'.")
    selected_index = int(answer)
    if selected_index < 1 or selected_index > len(choices):
        raise ValueError(f"Invalid {label} selection '{answer}'.")
    return choices[selected_index - 1]


def _choice_default_index(request: ChoiceRequest) -> int:
    if request.default_option_id is None:
        return 1
    for index, option in enumerate(request.options, start=1):
        if option.option_id == request.default_option_id:
            return index
    raise ValueError(f"Unknown default option '{request.default_option_id}'.")


def _format_summarize_choice(option: ChoiceOption, *, recommended: bool = False) -> str:
    label = option.label
    if option.detail:
        label = f"{label} | {option.detail}"
    if recommended:
        label = f"{label} [recommended]"
    return label


def _record_input(input_fn: Callable[[str], str] | None) -> Callable[[str], str] | None:
    if input_fn is not None:
        return input_fn
    if sys.stdin.isatty():
        return input
    return None


def _resolve_process_session_path(
    session_path: str | None,
    settings: AppSettings,
    *,
    input_fn: Callable[[str], str] | None = None,
) -> Path:
    if session_path is not None:
        return Path(session_path)
    candidates = processable_session_candidates(settings.paths.sessions_dir)
    if not candidates:
        raise ValueError("no recorded sessions found.")
    if len(candidates) == 1:
        return candidates[0].record_path
    reader = _record_input(input_fn)
    if reader is None:
        raise ValueError("multiple processable recordings found; pass an explicit session path.")
    return _prompt_choice(
        header="Processable recordings:",
        label="processable recordings",
        choices=candidates,
        formatter=_format_processable_session_candidate,
        input_fn=reader,
        default_index=1,
    ).record_path


def _format_processable_session_candidate(candidate: ProcessableSessionCandidate) -> str:
    label_parts = [
        candidate.name or "Untitled recording",
        f"completed {candidate.completed_at}",
        candidate.profile,
        f"id {candidate.session_id[:8]}",
    ]
    return f"{' | '.join(label_parts)} | {candidate.record_path}"


def _correction_handoff_available(session_path: Path) -> bool:
    resolved_session = resolve_session(session_path)
    session_id_value = session_id(resolved_session.record)
    store = FileSystemArtifactStore(
        session_id=session_id_value,
        session_dir=resolved_session.session_dir,
    )
    latest_semantic = _latest_semantic_processing_state(
        store=store,
        session_id_value=session_id_value,
    )
    return not (
        latest_semantic is not None
        and latest_semantic.status == "skipped"
        and latest_semantic.skip_reason == "provider_unavailable"
    )


def _latest_semantic_processing_state(
    *,
    store: ArtifactStore,
    session_id_value: str,
) -> ProcessingStageState | None:
    matches: list[tuple[int, ProcessingStageState]] = []
    for state_record in store.list(artifact_type=PROCESSING_STATE_ARTIFACT_TYPE):
        state = ProcessingStageState.from_dict(json.loads(store.retrieve(state_record.id)))
        if state.session_id != session_id_value or state.stage != SEMANTIC_COHERENCE_STAGE:
            continue
        try:
            sequence = int(Path(state_record.path).stem.split("-", 1)[0])
        except ValueError as exc:
            raise ProcessingStateError(
                "Processing state artifact name must start with sequence."
            ) from exc
        matches.append((sequence, state))
    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def _effective_record_data_root(
    data_root: Path | None,
    settings: AppSettings,
) -> Path | None:
    if data_root is not None:
        return data_root
    sessions_root = settings.paths.sessions_dir.parent
    if sessions_root != settings.paths.data_home:
        return sessions_root
    if xdg_data_home := os.environ.get("XDG_DATA_HOME"):
        xdg_data_root = Path(xdg_data_home).expanduser() / "mena"
        if sessions_root == xdg_data_root:
            return sessions_root
    return None


def _format_process_handoff_command(data_root: object) -> str:
    if isinstance(data_root, Path):
        return f"{DATA_ROOT_ENV_VAR}={quote(str(data_root))} mena process"
    return "mena process"


def _run_setup_command(
    *,
    check_only: bool,
    assume_yes: bool,
    authorize_local_fallback: bool,
    settings: AppSettings,
    input_fn: Callable[[str], str] | None,
    runner: CommandRunner | None,
    command_identity: CommandIdentity | None,
    hardware: HardwareFacts | None,
    summarization_provider: str = "local",
    mistral_summary_fallback: bool = False,
    accept_mistral_disclosure: bool = False,
) -> int:
    result = run_setup_workflow(
        settings=settings,
        options=SetupWorkflowOptions(
            check_only=check_only,
            assume_yes=assume_yes,
            authorize_local_fallback=authorize_local_fallback,
            summarization_provider=summarization_provider,
            mistral_summary_fallback=mistral_summary_fallback,
            accept_mistral_disclosure=accept_mistral_disclosure,
        ),
        interaction=_CliSetupInteraction(input_fn=input_fn),
        runner=runner,
        detector=DefaultSetupDetector(
            command_identity=command_identity,
            hardware=hardware,
        ),
    )
    for event in result.events:
        if isinstance(event, SetupReportEvent):
            print(event.render(), end="")
        elif isinstance(event, SetupMessageEvent):
            print(event.text)
    return result.exit_code


class _CliSetupInteraction(InteractionPort):
    def __init__(self, input_fn: Callable[[str], str] | None) -> None:
        self._input_fn = input if input_fn is None else input_fn

    def choose(self, request: ChoiceRequest) -> ChoiceOption:
        raise ValueError(f"Unsupported setup choice request: {request.topic}")

    def confirm(self, request: ConfirmationRequest) -> bool:
        if request.topic == "setup.primary_actions":
            prompt = "Proceed with automated setup actions? [y/N] "
        elif request.topic == "setup.fallback_actions":
            prompt = "Proceed with automated fallback setup actions? [y/N] "
        elif request.topic == "setup.mistral_summary_disclosure":
            # Render the disclosure here, not only via buffered workflow
            # events: setup events are printed after the workflow returns, so
            # the prompt must carry the disclosure itself to satisfy the
            # PDR-004 "disclose before selecting or authorizing" requirement.
            disclosure = request.context.get("disclosure", ())
            print("Mistral summarization disclosure:")
            if isinstance(disclosure, tuple):
                for line in disclosure:
                    print(f"  - {line}")
            role = str(request.context.get("role", "provider"))
            prompt = (
                f"Accept the Mistral summarization disclosure above and enable "
                f"Mistral as {role} provider? [y/N] "
            )
        else:
            raise ValueError(f"Unsupported setup confirmation request: {request.topic}")
        answer = self._input_fn(prompt)
        return answer.strip().lower() in {"y", "yes"}

    def request_text(self, request: TextRequest) -> str:
        raise ValueError(f"Unsupported setup text request: {request.topic}")

    def notify(self, notice: WorkflowNotice) -> None:
        return None


def _resolve_runtime_fallback_input(
    input_fn: Callable[[str], str] | None,
) -> Callable[[str], str] | None:
    """Choose the interactive runtime-fallback prompt, guarded by a TTY check.

    An explicit ``input_fn`` (tests, embedding) is honored as-is. Otherwise the
    real ``input`` builtin is used only when stdin is a TTY; on a non-TTY stdin we
    return ``None`` so an unauthorized runtime fallback raises the actionable
    ``RuntimeFallbackUnavailable`` ("pass --allow-runtime-fallback") instead of
    ``input()`` raising an opaque ``EOFError``.
    """

    if input_fn is not None:
        return input_fn
    return input if sys.stdin.isatty() else None


def _resolve_runtime_fallback_interaction(
    input_fn: Callable[[str], str] | None,
) -> InteractionPort | None:
    reader = _resolve_runtime_fallback_input(input_fn)
    if reader is None:
        return None
    return ConsoleProcessingInteraction(reader)


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
    interaction: InteractionPort | None = None,
) -> int:
    return summarize_workflow.run_summarization_command(
        session_path=session_path,
        manual_notes=manual_notes,
        language=language,
        settings=settings,
        setup_runner=setup_runner,
        setup_command_identity=setup_command_identity,
        setup_hardware=setup_hardware,
        allow_runtime_fallback=allow_runtime_fallback,
        runtime_profile_selector_fn=select_summarization_runtime_profile,
        runtime_profile_store_fn=store_runtime_profile_provenance,
        runtime_summarization_binder_fn=bind_summarization_runtime,
        run_summarization_stage_fn=run_summarization_stage,
        terminal_llm_release_fn=_record_terminal_local_llm_release,
        interaction=interaction,
    )


def _run_export_command(
    *,
    session_path: Path | None,
    summary_artifact_id: str | None,
    export_format: export_workflow.ExportFormat | None,
    settings: AppSettings,
    input_fn: Callable[[str], str] | None,
) -> int:
    result = export_workflow.run_export_workflow(
        export_workflow.ExportCommandInput(
            session_path=session_path,
            summary_artifact_id=summary_artifact_id,
            export_format=export_format,
        ),
        settings=settings,
        interaction=_resolve_export_interaction(input_fn),
    )
    if result.status == "cancelled":
        print("Export cancelled.")
        return 1
    return 0


def _settings_with_sessions_root(settings: AppSettings, sessions_root: Path) -> AppSettings:
    return replace(
        settings,
        paths=replace(settings.paths, sessions_dir=sessions_root),
    )


def _resolve_export_interaction(
    input_fn: Callable[[str], str] | None,
) -> InteractionPort:
    return ConsoleExportInteraction(_record_input(input_fn))


def _record_terminal_local_llm_release(
    *,
    store: FileSystemArtifactStore,
    run_id: str,
    attempt: int,
    provider: object,
    stage: str,
    boundary: str,
) -> RuntimeReleaseResult | None:
    result = release_terminal_local_llm(
        provider=provider,
        stage=stage,
        boundary=boundary,
    )
    if _release_skipped_without_observed_resource_use(result):
        return None
    store_runtime_stewardship_provenance(
        store=store,
        run_id=run_id,
        attempt=attempt,
        boundary=boundary,
        release_results=(result,),
    )
    if result.status in {"failed", "not_supported"}:
        print(f"Runtime stewardship warning: {result.detail}")
    else:
        print(f"Runtime stewardship: {result.detail}")
    return result


def _release_skipped_without_observed_resource_use(result: RuntimeReleaseResult) -> bool:
    return (
        result.status == "not_supported"
        and result.detail
        == "Provider did not observe Mena-owned runtime resource use for this lease."
    )


def _run_inspect_command(
    *,
    session_path: Path,
) -> int:
    report = verify_session_bundle(session_path)
    print(f"Integrity report for {session_path}: {'OK' if report.ok else 'FAILED'}")
    print(json.dumps(report.to_dict(), sort_keys=True, indent=2))
    return 0 if report.ok else 1


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
    try:
        return parse_utc_timestamp(value)
    except ValueError:
        return None


def _required_str(payload: dict[str, object] | object, key: str) -> str:
    if not isinstance(payload, dict):
        raise ValueError(f"Session field '{key}' is required and must be a non-empty string.")
    return required_str(
        payload,
        key,
        error=ValueError,
        message=f"Session field '{key}' is required and must be a non-empty string.",
    )


def _required_int(payload: dict[str, object], key: str) -> int:
    value = required_int(
        payload,
        key,
        error=ValueError,
        message=f"Session field '{key}' must be a positive integer.",
    )
    if value <= 0:
        raise ValueError(f"Session field '{key}' must be a positive integer.")
    return value


def _optional_metadata_str(payload: dict[str, object], key: str) -> str | None:
    return optional_str(
        payload,
        key,
        error=ValueError,
        message=f"Session field '{key}' must be a non-empty string or null.",
    )
