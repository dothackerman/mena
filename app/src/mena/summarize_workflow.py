"""Summarization orchestration extracted from the CLI layer."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from mena.artifacts import ArtifactRecord, ArtifactStore, FileSystemArtifactStore
from mena.correction_overlay import resolve_active_correction_overlay
from mena.manual_notes import ingest_manual_notes_file
from mena.merge_timing import (
    MERGED_TRANSCRIPT_ARTIFACT_TYPE,
    MergedTranscriptArtifact,
    MergeTimingContractError,
)
from mena.processing import ProcessingStageLedger
from mena.runtime_profile import (
    SUMMARIZATION_RUNTIME_STAGES,
    RuntimeProfileSelection,
    bind_summarization_runtime,
    select_runtime_profile,
    store_runtime_profile_provenance,
)
from mena.runtime_stewardship import RuntimeReleaseResult
from mena.sessions import (
    ResolvedSession,
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

RuntimeProfileSelector = Callable[..., RuntimeProfileSelection]
RuntimeProfileStoreFn = Callable[..., object]
RuntimeSummarizationBinderFn = Callable[..., object]
SummarizationStageFn = Callable[..., object]
TerminalReleaseFn = Callable[..., RuntimeReleaseResult | None]
ResolveSessionFn = Callable[[Path], ResolvedSession]


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
    runtime_profile_selector_fn: RuntimeProfileSelector = select_runtime_profile,
    runtime_profile_store_fn: RuntimeProfileStoreFn = store_runtime_profile_provenance,
    runtime_summarization_binder_fn: RuntimeSummarizationBinderFn = bind_summarization_runtime,
    run_summarization_stage_fn: SummarizationStageFn = run_summarization_stage,
    resolve_session_fn: ResolveSessionFn | None = None,
    terminal_llm_release_fn: TerminalReleaseFn | None = None,
) -> int:
    if resolve_session_fn is None:
        resolve_session_fn = resolve_session

    resolved_session = resolve_session_fn(session_path)
    session_dir = resolved_session.session_dir
    session_record = resolved_session.record
    session_id_value = session_id(session_record)
    summary_language = language or session_language_or_default(
        session_record,
        settings.summary.language,
    )
    summary_language = validate_summary_language(summary_language, "summary language")
    store = FileSystemArtifactStore(session_id=session_id_value, session_dir=session_dir)

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
    runtime_selection: RuntimeProfileSelection = runtime_profile_selector_fn(
        settings=settings,
        runner=setup_runner,
        command_identity=setup_command_identity,
        hardware=setup_hardware,
        allow_runtime_fallback=allow_runtime_fallback,
        required_stages=SUMMARIZATION_RUNTIME_STAGES,
    )

    run_id = f"summarize-{uuid4()}"
    runtime_profile_store_fn(
        store=store,
        run_id=run_id,
        attempt=1,
        selection=runtime_selection,
    )

    provider = runtime_summarization_binder_fn(
        selection=runtime_selection,
        settings=settings,
    )

    ledger = ProcessingStageLedger(store=store, session_id=session_id_value, run_id=run_id)
    try:
        result = cast(
            SummarizationStageResult,
            run_summarization_stage_fn(
                store=store,
                ledger=ledger,
                merged_transcript_record=merged_transcript_record,
                correction_overlay_record=resolved_overlay[0],
                manual_notes_record=manual_notes_record,
                provider=provider,
                policy=SummarizationPolicy(language=summary_language),
            ),
        )
    finally:
        if terminal_llm_release_fn is not None:
            terminal_llm_release_fn(
                store=store,
                run_id=run_id,
                attempt=1,
                provider=provider,
                stage="summarization",
                boundary="summarize",
            )

    print(f"Summary written to: {result.summary_record.path}")
    return 0


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


_run_summarization_command = run_summarization_command
