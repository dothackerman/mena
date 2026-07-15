"""``mena record`` parser and console handler."""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from shlex import quote
from typing import cast

from mena.cli._console import _prompt_choice, _resolve_console_input
from mena.cli.command import bind_handler
from mena.cli.composition import CommandContext
from mena.discovery import (
    AudioDiscovery,
    format_discovery,
)
from mena.interaction import (
    ChoiceOption,
    ChoiceRequest,
    ConfirmationRequest,
    TextRequest,
    WorkflowNotice,
)
from mena.profiles import (
    CaptureProfileSelection,
    capture_profile_choices,
    parse_capture_profile,
)
from mena.protocol import (
    StartCapturePayload,
)
from mena.recording_control import (
    WORKER_EXIT_TIMEOUT_SECONDS,
    RecordCommandInput,
    format_record_diagnostics,
    run_recording_control_workflow,
)
from mena.session_lock import SessionLockContention
from mena.sessions import (
    read_session_record,
)
from mena.settings import DATA_ROOT_ENV_VAR, AppSettings, validate_summary_language


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    **_: object,
) -> None:
    parser = subparsers.add_parser("record", help="Create a recording session.")
    parser.add_argument(
        "--profile",
        choices=capture_profile_choices(),
        help="Capture profile for this recording session.",
    )
    parser.add_argument("--name", help="Friendly session name for human reference.")
    parser.add_argument(
        "--language", default=None, help="Meeting language to persist as the session language."
    )
    parser.add_argument("--mic", default=None, help="Microphone route id to capture.")
    parser.add_argument(
        "--playback-stream",
        default=None,
        help="Playback stream route id to capture for online meetings.",
    )
    parser.add_argument("--output-sink", default=None, help="Meeting audio output route id.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Create a session and print read-only audio discovery without starting capture.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print route planning details and write worker capture diagnostics to stderr.",
    )
    parser.add_argument(
        "--worker-exit-timeout",
        type=float,
        default=WORKER_EXIT_TIMEOUT_SECONDS,
        help="Seconds to wait for capture worker teardown after stop_capture.",
    )
    bind_handler(parser, run)


def run(args: argparse.Namespace, context: CommandContext) -> int:
    dependencies = context.dependencies
    try:
        profile = _resolve_record_profile(args.profile, input_fn=dependencies.input_fn)
        language = _resolve_record_language(
            args.language,
            default_language=context.settings.summary.language,
            input_fn=dependencies.input_fn,
            dry_run=args.dry_run,
        )
    except ValueError as exc:
        print(f"Cannot start recording: {exc}")
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Recording setup cancelled.")
        return 1
    try:
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
                stop_input_available=_resolve_console_input(dependencies.input_fn) is not None,
            ),
            data_root=_effective_record_data_root(dependencies.data_root, context.settings),
            discoverer=dependencies.discoverer,
            worker_factory=dependencies.worker_factory,
            interaction=ConsoleRecordInteraction(dependencies.input_fn),
            now=dependencies.now,
        )
    except SessionLockContention as exc:
        print(f"Cannot start recording: {exc}")
        return 4
    return result.exit_code


class ConsoleRecordInteraction:
    """Console adapter for record workflow interaction topics."""

    def __init__(self, input_fn: Callable[[str], str] | None) -> None:
        self._input_fn = input_fn

    def choose(self, request: ChoiceRequest) -> ChoiceOption:
        reader = _resolve_console_input(self._input_fn)
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
        reader = _resolve_console_input(self._input_fn)
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


def _resolve_record_profile(
    raw_profile: str | None,
    *,
    input_fn: Callable[[str], str] | None,
) -> CaptureProfileSelection:
    if raw_profile is not None:
        return parse_capture_profile(raw_profile)
    reader = _resolve_console_input(input_fn)
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
    reader = _resolve_console_input(input_fn)
    if reader is None:
        raise ValueError("Meeting language is required before recording; pass --language.")
    answer = reader(f"Meeting language [{default_language}]: ").strip() or default_language
    return validate_summary_language(answer, "meeting language")


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
