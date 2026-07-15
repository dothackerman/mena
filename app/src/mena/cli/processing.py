"""Shared process/resume console execution."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from shlex import quote
from typing import cast
from uuid import uuid4

from mena import processing_orchestration
from mena.artifacts import (
    ArtifactStore,
    ArtifactStoreError,
    FileSystemArtifactStore,
)
from mena.cli._console import _prompt_choice, _resolve_console_input
from mena.cli._runtime_fallback import _print_runtime_stewardship_release_notice
from mena.cli.composition import CommandContext
from mena.diarization import DiarizationError
from mena.interaction import (
    ChoiceOption,
    ChoiceRequest,
    ConfirmationRequest,
    InteractionPort,
    TextRequest,
    WorkflowNotice,
)
from mena.merge_timing import MergeTimingError
from mena.processing import (
    PROCESSING_STATE_ARTIFACT_TYPE,
    ProcessingStageState,
    ProcessingStateError,
)
from mena.provider_validation import ProviderValidationError
from mena.review_flagging import ReviewFlaggingError
from mena.runtime_profile import RuntimeFallbackUnavailable
from mena.runtime_stewardship import (
    RUNTIME_STEWARDSHIP_RELEASE_NOTICE_TOPIC,
    RuntimeReleaseResult,
)
from mena.semantic_coherence import (
    SEMANTIC_COHERENCE_STAGE,
    SemanticCoherenceError,
)
from mena.session_lock import SessionLockContention
from mena.sessions import (
    ProcessableSessionCandidate,
    processable_session_candidates,
    resolve_session,
    session_id,
)
from mena.settings import AppSettings
from mena.setup import SetupEvaluation
from mena.transcription import TranscriptionError


def run(args: argparse.Namespace, context: CommandContext, *, resume: bool) -> int:
    dependencies = context.dependencies
    try:
        if resume:
            session_path = Path(args.session_path)
            exit_code = processing_orchestration.run_resume_processing_command(
                session_path=session_path,
                now=dependencies.now,
                transcription_provider_factory=dependencies.transcription_provider_factory,
                diarization_provider_factory=dependencies.diarization_provider_factory,
                semantic_coherence_provider_factory=dependencies.semantic_coherence_provider_factory,
                settings=context.settings,
                setup_runner=dependencies.setup_runner,
                setup_command_identity=dependencies.setup_command_identity,
                setup_hardware=dependencies.setup_hardware,
                allow_runtime_fallback=args.allow_runtime_fallback,
                interaction=_resolve_processing_interaction(dependencies.input_fn),
            )
        else:
            session_path = _resolve_process_session_path(
                args.session_path, context.settings, input_fn=dependencies.input_fn
            )
            print(f"Processing recording: {session_path}")
            exit_code = dependencies.processing_command_runner(
                session_path=session_path,
                run_id=dependencies.run_id or str(uuid4()),
                attempt=1,
                now=dependencies.now,
                transcription_provider_factory=dependencies.transcription_provider_factory,
                diarization_provider_factory=dependencies.diarization_provider_factory,
                semantic_coherence_provider_factory=dependencies.semantic_coherence_provider_factory,
                settings=context.settings,
                setup_runner=dependencies.setup_runner,
                setup_command_identity=dependencies.setup_command_identity,
                setup_hardware=dependencies.setup_hardware,
                allow_runtime_fallback=args.allow_runtime_fallback,
                interaction=_resolve_processing_interaction(dependencies.input_fn),
                orchestrator_command="process",
            )
        if not resume and exit_code == 0:
            print("Processing completed successfully.")
            if _correction_handoff_available(session_path):
                print(f"Next command: mena correct {quote(str(session_path))}")
        return exit_code
    except processing_orchestration.ProcessingResumeUnavailable as exc:
        print(str(exc))
        return 1
    except RuntimeFallbackUnavailable as exc:
        print(f"Cannot run processing workflow: {exc}")
        return 3
    except SessionLockContention as exc:
        print(f"Cannot run processing workflow: {exc}")
        return 4
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


class ConsoleProcessingInteraction:
    """Console adapter for process and resume workflow interaction topics."""

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


def _resolve_processing_interaction(
    input_fn: Callable[[str], str] | None,
) -> InteractionPort:
    return ConsoleProcessingInteraction(_resolve_console_input(input_fn))


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
    reader = _resolve_console_input(input_fn)
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
