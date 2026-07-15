"""CLI adapter for the mena PoC."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from shlex import quote
from typing import Any, TypeVar, cast

from mena import export_workflow, summarize_workflow
from mena.artifacts import (
    ArtifactStore,
    FileSystemArtifactStore,
)
from mena.discovery import (
    AudioDiscovery,
    format_discovery,
)
from mena.interaction import (
    ChoiceOption,
    ChoiceRequest,
    ConfirmationRequest,
    InteractionPort,
    TextRequest,
    WorkflowNotice,
)
from mena.processing import (
    PROCESSING_STATE_ARTIFACT_TYPE,
    ProcessingStageState,
    ProcessingStateError,
)
from mena.profiles import (
    CaptureProfileSelection,
    parse_capture_profile,
)
from mena.protocol import (
    StartCapturePayload,
)
from mena.recording_control import (
    format_record_diagnostics,
)
from mena.runtime_stewardship import (
    RUNTIME_STEWARDSHIP_RELEASE_NOTICE_TOPIC,
    RuntimeReleaseResult,
)
from mena.semantic_coherence import (
    SEMANTIC_COHERENCE_STAGE,
)
from mena.session_integrity import (
    SessionIndexEntry,
    rebuild_session_index,
    verify_session_bundle,
)
from mena.sessions import (
    ProcessableSessionCandidate,
    processable_session_candidates,
    read_session_record,
    resolve_session,
    session_id,
)
from mena.settings import DATA_ROOT_ENV_VAR, AppSettings, validate_summary_language
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
from mena.timestamps import parse_utc_timestamp

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
UNIMPLEMENTED_COMMANDS = ("show",)
WORKER_EXIT_TIMEOUT_SECONDS = 30.0
ProviderFactory = Callable[[], Any]
ChoiceT = TypeVar("ChoiceT")


def _print_runtime_stewardship_release_notice(result: RuntimeReleaseResult) -> None:
    if result.status in {"failed", "not_supported"}:
        print(f"Runtime stewardship warning: {result.detail}")
    else:
        print(f"Runtime stewardship: {result.detail}")


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

    def __init__(self, input_fn: Callable[[str], str] | None) -> None:
        self._input_fn = input_fn

    @property
    def runtime_fallback_confirmation_available(self) -> bool:
        return self._input_fn is not None

    def choose(self, request: ChoiceRequest) -> ChoiceOption:
        raise ValueError(f"Unsupported processing choice topic '{request.topic}'.")

    def confirm(self, request: ConfirmationRequest) -> bool:
        if request.topic != "runtime_fallback.one_run":
            raise ValueError(f"Unsupported processing confirmation topic '{request.topic}'.")
        if self._input_fn is None:
            return False
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
        if notice.topic != RUNTIME_STEWARDSHIP_RELEASE_NOTICE_TOPIC:
            raise ValueError(f"Unsupported processing notice topic '{notice.topic}'.")
        _print_runtime_stewardship_release_notice(
            cast(RuntimeReleaseResult, notice.context["result"])
        )


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
        elif topic == RUNTIME_STEWARDSHIP_RELEASE_NOTICE_TOPIC:
            _print_runtime_stewardship_release_notice(cast(RuntimeReleaseResult, context["result"]))
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
) -> InteractionPort:
    reader = _resolve_runtime_fallback_input(input_fn)
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
    summarization_provider_factory: ProviderFactory | None = None,
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
        summarization_provider_factory=summarization_provider_factory,
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


UNTITLED_SESSION_NAME = "Untitled recording"


def _run_list_command(*, sessions_root: Path) -> int:
    """Render the rebuildable global-index projection as one line per session.

    The workflow interface is the existing index projection
    (``rebuild_session_index``); this command is a rendering adapter over it and
    never persists the index to disk (ADR-018). Sorting is a rendering concern:
    the projection returns entries in bundle-path order, so newest-first ordering
    is applied here.
    """

    entries = rebuild_session_index(sessions_root)
    if not entries:
        print("No sessions found.")
        return 0
    for entry in _sort_session_index_newest_first(entries):
        print(_format_session_index_line(entry))
    return 0


def _sort_session_index_newest_first(
    entries: Sequence[SessionIndexEntry],
) -> list[SessionIndexEntry]:
    parseable: list[tuple[datetime, SessionIndexEntry]] = []
    unparseable: list[SessionIndexEntry] = []
    for entry in entries:
        parsed = _parse_iso_timestamp(entry.created_at)
        if parsed is None:
            unparseable.append(entry)
        else:
            parseable.append((parsed, entry))
    # Stable sort keyed only on the timestamp keeps entries with equal (or
    # unparseable) timestamps in the projection's original order.
    parseable.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in parseable] + unparseable


def _format_session_index_line(entry: SessionIndexEntry) -> str:
    fields = (
        entry.created_at,
        entry.session_id[:8],
        entry.name or UNTITLED_SESSION_NAME,
        entry.profile,
        entry.status,
        str(entry.bundle_path),
    )
    line = " | ".join(fields)
    if not entry.integrity_ok:
        reason_suffix = ""
        if entry.finding_reasons:
            reason_suffix = ": " + "; ".join(entry.finding_reasons)
        line = f"{line} | DEGRADED ({entry.finding_count} integrity finding(s){reason_suffix})"
    return line


def _run_inspect_command(
    *,
    session_path: Path,
) -> int:
    report = verify_session_bundle(session_path)
    print(f"Integrity report for {session_path}: {'OK' if report.ok else 'FAILED'}")
    print(json.dumps(report.to_dict(), sort_keys=True, indent=2))
    return 0 if report.ok else 1


def _parse_iso_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return parse_utc_timestamp(value)
    except ValueError:
        return None
