"""Summarization orchestration extracted from the CLI layer."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from shlex import quote
from typing import cast
from uuid import uuid4

from mena.artifacts import (
    ArtifactRecord,
    ArtifactStore,
    ArtifactStoreError,
    FileSystemArtifactStore,
)
from mena.correction_overlay import (
    CorrectionOverlayContractError,
    resolve_active_correction_overlay,
)
from mena.interaction import (
    ChoiceOption,
    ChoiceRequest,
    ConfirmationRequest,
    InteractionPort,
    WorkflowNotice,
)
from mena.manual_notes import ingest_manual_notes_file
from mena.merge_timing import (
    MERGED_TRANSCRIPT_ARTIFACT_TYPE,
    MergedTranscriptArtifact,
    MergeTimingContractError,
)
from mena.mistral_llm import MISTRAL_SUMMARY_DISCLOSURE, MistralSummaryState
from mena.processing import ProcessingStageLedger
from mena.runtime_profile import (
    ProviderFactoryInput,
    run_summarization_with_runtime_fallback,
)
from mena.runtime_stewardship import (
    RUNTIME_STEWARDSHIP_RELEASE_NOTICE_TOPIC,
    RuntimeReleaseResult,
)
from mena.sessions import (
    SESSION_RECORD_FILENAME,
    resolve_session,
    session_id,
    session_language_or_default,
)
from mena.settings import AppSettings, validate_summary_language
from mena.setup import CommandIdentity, CommandRunner, HardwareFacts
from mena.summarization import (
    SummarizationError,
    SummarizationPolicy,
    SummarizationStageResult,
    run_summarization_stage,
)
from mena.timestamps import parse_utc_timestamp


@dataclass(frozen=True)
class SummarizeSessionOption:
    session_dir: Path
    created_at: datetime | None
    name: str | None


def resolve_summarize_session_path(
    *,
    explicit_session_path: str | None,
    sessions_root: Path,
    interaction: InteractionPort | None = None,
) -> Path:
    if explicit_session_path is not None:
        return Path(explicit_session_path)

    candidates = find_ready_summarize_sessions(sessions_root=sessions_root)
    if len(candidates) == 0:
        raise ValueError(
            "No ready sessions were found for summarization. "
            "Run process and correction first, then retry with an explicit session path."
        )
    if len(candidates) == 1:
        selected = candidates[0]
    else:
        selected = _choose_summarize_session(options=candidates, interaction=interaction)

    _notify_summarize_session_selected(
        interaction=interaction,
        selected=selected,
        candidate_count=len(candidates),
    )
    return selected.session_dir


def find_ready_summarize_sessions(
    *,
    sessions_root: Path,
) -> tuple[SummarizeSessionOption, ...]:
    try:
        if not sessions_root.exists():
            return ()
        session_dirs = sorted(
            session_dir for session_dir in sessions_root.iterdir() if session_dir.is_dir()
        )
    except OSError:
        return ()

    options: list[SummarizeSessionOption] = []
    for session_dir in session_dirs:
        if not (session_dir / SESSION_RECORD_FILENAME).is_file():
            continue
        option = _resolve_summarize_session_candidate(session_dir=session_dir)
        if option is not None:
            options.append(option)
    return tuple(
        sorted(
            options,
            key=lambda option: (
                option.created_at or datetime.min.replace(tzinfo=UTC),
                option.session_dir.name,
            ),
            reverse=True,
        )
    )


def run_summarization_command(
    *,
    session_path: Path,
    manual_notes: str | None,
    language: str | None,
    settings: AppSettings,
    setup_runner: CommandRunner | None = None,
    setup_command_identity: CommandIdentity | None = None,
    setup_hardware: HardwareFacts | None = None,
    allow_runtime_fallback: bool = False,
    summarization_provider_factory: ProviderFactoryInput = None,
    interaction: InteractionPort | None = None,
) -> int:
    resolved_session = resolve_session(session_path)
    session_dir = resolved_session.session_dir
    session_record = resolved_session.record
    session_id_value = session_id(session_record)
    summary_language = language or session_language_or_default(
        session_record,
        settings.summary.language,
    )
    summary_language = validate_summary_language(summary_language, "summary language")
    store = FileSystemArtifactStore(session_id=session_id_value, session_dir=session_dir)

    _notify_or_print(
        interaction=interaction,
        notice=WorkflowNotice(
            topic="summarize.started",
            context={"session_dir": session_dir},
        ),
        fallback="Creating meeting notes...",
    )

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
    authorize_remote_fallback = _remote_fallback_authorizer(interaction)

    run_id = f"summarize-{uuid4()}"
    ledger = ProcessingStageLedger(store=store, session_id=session_id_value, run_id=run_id)
    observed_release_results: list[RuntimeReleaseResult] = []

    def _observe_runtime_release(result: RuntimeReleaseResult) -> None:
        observed_release_results.append(result)
        _notify_runtime_stewardship_release(
            interaction=interaction,
            result=result,
        )

    runtime_result = run_summarization_with_runtime_fallback(
        settings=settings,
        store=store,
        run_id=run_id,
        attempt=1,
        runner=setup_runner,
        command_identity=setup_command_identity,
        hardware=setup_hardware,
        allow_runtime_fallback=allow_runtime_fallback,
        authorize_remote_summarization_fallback=authorize_remote_fallback,
        failed_input_artifacts=_stage_input_artifact_ids(
            merged_transcript_record,
            resolved_overlay[0],
            manual_notes_record,
        ),
        summarization_provider_factory=summarization_provider_factory,
        runtime_release_observer=_observe_runtime_release,
        run=lambda provider, attempt: run_summarization_stage(
            store=store,
            ledger=ledger,
            merged_transcript_record=merged_transcript_record,
            correction_overlay_record=resolved_overlay[0],
            manual_notes_record=manual_notes_record,
            provider=provider,
            policy=SummarizationPolicy(language=summary_language),
            attempt=attempt,
        ),
    )
    already_observed = list(observed_release_results)
    for release_result in runtime_result.release_results:
        if release_result in already_observed:
            already_observed.remove(release_result)
            continue
        _notify_runtime_stewardship_release(
            interaction=interaction,
            result=release_result,
        )
    result = cast(SummarizationStageResult, runtime_result.value)

    _notify_or_print(
        interaction=interaction,
        notice=WorkflowNotice(
            topic="summarize.completed",
            context={
                "summary_path": result.summary_record.path,
                "summary_artifact_id": result.summary_record.id,
                "session_dir": resolved_session.session_dir,
            },
        ),
        fallback=(
            f"Notes created: {result.summary_record.path}\n"
            f"Session path: {resolved_session.session_dir}\n"
            "Next command: "
            f"mena export --summary-artifact-id {quote(result.summary_record.id)} "
            f"{quote(str(resolved_session.session_dir))}"
        ),
    )
    return 0


def _remote_fallback_authorizer(
    interaction: InteractionPort | None,
) -> Callable[[str, MistralSummaryState], bool]:
    """Interactive authorization for a not-pre-authorized remote fallback transition.

    Without an interaction port the transition is declined: a non-interactive
    run must never move summarization text to a remote provider that setup did
    not pre-authorize.
    """

    def _authorize(reason: str, state: MistralSummaryState) -> bool:
        if interaction is None:
            return False
        return interaction.confirm(
            ConfirmationRequest(
                topic="runtime_fallback.summarization_remote",
                context={
                    "reason": reason,
                    "provider": "Mistral",
                    "request_model": state.request_model,
                    "resolved_model": state.resolved_model,
                    "disclosure": MISTRAL_SUMMARY_DISCLOSURE,
                },
                default=False,
            )
        )

    return _authorize


def _stage_input_artifact_ids(
    merged_transcript_record: ArtifactRecord,
    correction_overlay_record: ArtifactRecord,
    manual_notes_record: ArtifactRecord | None,
) -> tuple[str, ...]:
    ids = [merged_transcript_record.id, correction_overlay_record.id]
    if manual_notes_record is not None:
        ids.append(manual_notes_record.id)
    return tuple(ids)


def _notify_runtime_stewardship_release(
    *,
    interaction: InteractionPort | None,
    result: RuntimeReleaseResult,
) -> None:
    if interaction is None:
        return
    interaction.notify(
        WorkflowNotice(
            topic=RUNTIME_STEWARDSHIP_RELEASE_NOTICE_TOPIC,
            severity=("warning" if result.status in {"failed", "not_supported"} else "info"),
            context={"result": result},
        )
    )


def _choose_summarize_session(
    *,
    options: tuple[SummarizeSessionOption, ...],
    interaction: InteractionPort | None,
) -> SummarizeSessionOption:
    newest = options[0]
    if interaction is None:
        return newest

    selected = interaction.choose(
        ChoiceRequest(
            topic="summarize.session",
            options=tuple(
                ChoiceOption(
                    option_id=_summarize_session_option_id(option),
                    label=_summarize_session_label(option),
                    detail=str(option.session_dir),
                )
                for option in options
            ),
            default_option_id=_summarize_session_option_id(newest),
        )
    )
    for option in options:
        if _summarize_session_option_id(option) == selected.option_id:
            return option
    raise ValueError("Selected summarize session is no longer available.")


def _notify_summarize_session_selected(
    *,
    interaction: InteractionPort | None,
    selected: SummarizeSessionOption,
    candidate_count: int,
) -> None:
    if interaction is None:
        return
    interaction.notify(
        WorkflowNotice(
            topic="summarize.session_selected",
            context={
                "session_dir": selected.session_dir,
                "label": _summarize_session_label(selected),
                "candidate_count": candidate_count,
            },
        )
    )


def _resolve_summarize_session_candidate(
    *,
    session_dir: Path,
) -> SummarizeSessionOption | None:
    try:
        resolved_session = resolve_session(session_dir)
    except ValueError:
        return None

    try:
        session_record = resolved_session.record
        session_id_value = session_id(session_record)
        store = FileSystemArtifactStore(session_id=session_id_value, session_dir=session_dir)
        merged_record = _resolve_latest_merged_transcript_record(store=store)
        if merged_record is None:
            return None
        if (
            resolve_active_correction_overlay(
                store=store,
                source_transcript_record=merged_record,
            )
            is None
        ):
            return None
    except (CorrectionOverlayContractError, ArtifactStoreError, OSError, ValueError):
        return None

    raw_name = session_record.get("name")
    if not isinstance(raw_name, str):
        raw_name = None

    return SummarizeSessionOption(
        session_dir=session_dir,
        created_at=_parse_session_created_at(session_record=session_record),
        name=raw_name,
    )


def _summarize_session_option_id(option: SummarizeSessionOption) -> str:
    return option.session_dir.as_posix()


def _summarize_session_label(option: SummarizeSessionOption) -> str:
    created_at = (
        option.created_at.isoformat(timespec="seconds") if option.created_at is not None else "?"
    )
    if option.name:
        return f"{option.session_dir.name} ({option.name}, {created_at})"
    return f"{option.session_dir.name} ({created_at})"


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
    try:
        if not store.verify(record.id).ok:
            return None
        merged = MergedTranscriptArtifact.from_dict(
            json.loads(store.retrieve(record.id).decode("utf-8"))
        )
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, MergeTimingContractError):
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


def _parse_session_created_at(*, session_record: Mapping[str, object]) -> datetime | None:
    raw = session_record.get("created_at")
    if not isinstance(raw, str):
        return None
    return _parse_iso_timestamp(raw)


def _notify_or_print(
    *,
    interaction: InteractionPort | None,
    notice: WorkflowNotice,
    fallback: str,
) -> None:
    if interaction is not None:
        interaction.notify(notice)
        return
    print(fallback)


_run_summarization_command = run_summarization_command
