import json
from dataclasses import replace
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from mena.artifacts import ArtifactRecord, FileSystemArtifactStore, InMemoryArtifactStore
from mena.correction_overlay import (
    CorrectionEntry,
    create_correction_overlay,
    persist_correction_overlay_artifact,
)
from mena.manual_notes import ManualNotesIngestionError, ingest_manual_notes_file
from mena.merge_timing import (
    MERGED_TRANSCRIPT_SCHEMA_VERSION,
    MergedTranscriptArtifact,
    MergedTranscriptSegment,
    MergedTranscriptSource,
    MergePolicy,
    SegmentConfidence,
    SpeakerIdentity,
    persist_merged_transcript_artifact,
)
from mena.ollama_summarization import (
    OLLAMA_SUMMARIZATION_NUM_CTX,
    OLLAMA_SUMMARIZATION_SEED,
    OllamaSummarizationProvider,
)
from mena.processing import ArtifactRef, ProcessingStageLedger
from mena.runtime_stewardship import RuntimeLease
from mena.summarization import (
    PROMPT_POLICY,
    REQUIRED_NOTE_PRODUCTS,
    SCHEMA_PROMPT_POLICY,
    SUMMARIZATION_STAGE,
    SUMMARY_SCHEMA_VERSION,
    FakeSummarizationProvider,
    ProductRequestRecord,
    SummarizationContractError,
    SummarizationError,
    SummarizationPolicy,
    SummarizationProviderMetadata,
    SummarizationProviderUnavailable,
    SummarizationRequest,
    SummarizationTokenBudgetError,
    SummaryActionItem,
    SummaryArtifact,
    SummaryResult,
    SummaryTimelineEntry,
    persist_summary_artifact,
    run_summarization_stage,
)

SESSION_ID = "00000000-0000-0000-0000-000000000001"
RUN_ID = "run-20260518-summary"
NOW = datetime(2026, 5, 18, 17, 0, 0, tzinfo=UTC)


def test_summarization_facade_does_not_expose_ollama_adapter_details() -> None:
    import mena.summarization as summarization

    assert "OllamaSummarizationProvider" not in summarization.__all__
    assert not hasattr(summarization, "OllamaSummarizationProvider")
    assert not hasattr(summarization, "OLLAMA_SUMMARIZATION_NUM_CTX")
    assert not hasattr(summarization, "OLLAMA_SUMMARIZATION_SEED")
    assert not hasattr(summarization, "OLLAMA_SUMMARIZATION_TEMPERATURE")
    assert not hasattr(summarization, "OLLAMA_DETERMINISTIC_OPTIONS")


@pytest.mark.parametrize(
    ("language", "expected_headings", "forbidden_heading"),
    [
        ("en", ["## Timeline", "## Action items", "## General summary"], "## Zeitverlauf"),
        ("de", ["## Zeitverlauf", "## Aufgaben", "## Zusammenfassung"], "## Timeline"),
    ],
)
def test_summarize_to_export_path_renders_three_product_package(
    tmp_path: Path,
    language: str,
    expected_headings: list[str],
    forbidden_heading: str,
) -> None:
    from mena.export import export_summary_markdown

    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    merged_record = _persist_merged_transcript(store=store)
    provider = FakeSummarizationProvider(result=_summary_result())
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    stage_result = run_summarization_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        provider=provider,
        policy=SummarizationPolicy(language=language),
        now=NOW,
    )

    export_result = export_summary_markdown(store=store)
    markdown = store.retrieve(export_result.export_record.id).decode("utf-8")
    headings = [line for line in markdown.splitlines() if line.startswith("## ")]

    assert stage_result.summary.version == SUMMARY_SCHEMA_VERSION
    assert not hasattr(stage_result.summary, "important_points")
    assert headings == expected_headings
    assert forbidden_heading not in markdown
    assert "Important points" not in markdown
    assert "Wichtige Punkte" not in markdown


def test_summarization_stage_persists_summary_and_checkpoint(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    merged_record = _persist_merged_transcript(store=store)
    notes_record = _persist_notes(store=store)
    provider = FakeSummarizationProvider(result=_summary_result())
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    result = run_summarization_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        manual_notes_record=notes_record,
        provider=provider,
        policy=SummarizationPolicy(language="de-CH"),
        now=NOW,
    )

    assert result.skipped is False
    assert result.state.status == "completed"
    assert result.summary_record.artifact_type == "summary"
    assert result.summary_record.metadata["contract"] == "summary.v3"
    assert result.summary_record.metadata["summary_created_at"] == "2026-05-18T17:00:00Z"
    payload = json.loads(store.retrieve(result.summary_record.id))
    Draft202012Validator(_load_summary_schema(), format_checker=FormatChecker()).validate(payload)
    artifact = SummaryArtifact.from_dict(payload)
    assert artifact.provider.name == "fake"
    assert artifact.policy["prompt_policy"] == PROMPT_POLICY
    assert artifact.provider.prompt_policy == PROMPT_POLICY
    assert "provider_config_id" not in artifact.policy
    assert artifact.timeline[0].text == "Discussed database migration before Friday."
    assert artifact.timeline[0].start_seconds == 0.0
    assert artifact.action_items[0].owner == "You"
    assert provider.requests[0].manual_notes == "- Manual note: release is blocked."
    assert provider.requests[0].policy.language == "de-CH"
    assert [state.status for state in ledger.states(stage=SUMMARIZATION_STAGE)] == [
        "running",
        "completed",
    ]


def test_summarization_stage_ignores_manual_notes_when_policy_excludes_them() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(store=store)
    notes_record = store.store(
        artifact_type="manual_notes",
        name="invalid-notes.md",
        data=b"\xff",
        version=1,
        metadata={"contract": "manual-notes.raw"},
    )
    provider = FakeSummarizationProvider(result=_summary_result())
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    result = run_summarization_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        manual_notes_record=notes_record,
        provider=provider,
        policy=SummarizationPolicy(include_manual_notes=False),
        now=NOW,
    )

    assert provider.requests[0].manual_notes is None
    assert result.summary.manual_notes_artifact is None
    assert result.summary_record.metadata["manual_notes_artifact_id"] is None
    assert result.state.input_artifacts == (ArtifactRef.from_record(merged_record),)


def test_manual_notes_file_boundary_feeds_summarization_input(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    merged_record = _persist_merged_transcript(store=store)
    notes_input_path = tmp_path / "inputs" / "notes.md"
    notes_input_path.parent.mkdir()
    notes_input_path.write_text("- Manual note from file.", encoding="utf-8")
    provider = FakeSummarizationProvider(result=_summary_result())
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    notes_record = ingest_manual_notes_file(store=store, notes_path=notes_input_path)
    result = run_summarization_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        manual_notes_record=notes_record,
        provider=provider,
        now=NOW,
    )

    assert notes_record is not None
    assert result.summary.manual_notes_artifact == ArtifactRef.from_record(notes_record)
    assert provider.requests[0].manual_notes == "- Manual note from file."


def test_summarization_stage_preserves_transcript_only_when_notes_file_absent() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(store=store)
    provider = FakeSummarizationProvider(result=_summary_result())
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    notes_record = ingest_manual_notes_file(store=store, notes_path=None)
    result = run_summarization_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        manual_notes_record=notes_record,
        provider=provider,
        now=NOW,
    )

    assert notes_record is None
    assert provider.requests[0].manual_notes is None
    assert result.summary.manual_notes_artifact is None
    assert store.list(artifact_type="manual_notes") == ()


def test_manual_notes_file_boundary_fails_before_provider_invocation(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    merged_record = _persist_merged_transcript(store=store)
    provider = FakeSummarizationProvider(result=_summary_result())
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    def run_with_notes_file(notes_path: Path) -> None:
        notes_record = ingest_manual_notes_file(store=store, notes_path=notes_path)
        run_summarization_stage(
            store=store,
            ledger=ledger,
            merged_transcript_record=merged_record,
            manual_notes_record=notes_record,
            provider=provider,
            now=NOW,
        )

    with pytest.raises(ManualNotesIngestionError, match="does not exist"):
        run_with_notes_file(tmp_path / "missing.md")

    assert provider.requests == []
    assert ledger.states(stage=SUMMARIZATION_STAGE) == ()


def test_summarization_stage_reuses_verified_completed_output() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(store=store)
    provider = FakeSummarizationProvider(result=_summary_result())
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    first = run_summarization_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        provider=provider,
        now=NOW,
    )
    second = run_summarization_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        provider=provider,
        now=NOW,
    )

    assert second.skipped is True
    assert second.skip_reason == "completed_output_reused"
    assert second.summary_record == first.summary_record


def test_summarization_stage_does_not_reuse_old_prompt_policy_completed_output() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(store=store)
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    old_result = _summary_result()
    old_provider_metadata = replace(
        FakeSummarizationProvider(result=old_result).metadata,
        prompt_policy=SCHEMA_PROMPT_POLICY,
    )
    old_artifact = SummaryArtifact(
        version=SUMMARY_SCHEMA_VERSION,
        session_id=SESSION_ID,
        merged_transcript_artifact=ArtifactRef.from_record(merged_record),
        manual_notes_artifact=None,
        policy={
            "policy_version": 1,
            "prompt_policy": SCHEMA_PROMPT_POLICY,
            "language": "de",
            "include_manual_notes": True,
        },
        provider=old_provider_metadata,
        timeline=old_result.timeline,
        action_items=old_result.action_items,
        general_summary=old_result.general_summary,
        created_at="2026-05-18T17:00:00Z",
        request_records=old_result.request_records,
    )
    old_record = persist_summary_artifact(
        store=store,
        artifact=old_artifact,
        correction_overlay_record=None,
        name="summary/old-prompt-policy.json",
    )
    input_artifacts = (ArtifactRef.from_record(merged_record),)
    running = ledger.mark_running(
        stage=SUMMARIZATION_STAGE,
        input_artifacts=input_artifacts,
        now=NOW,
    )
    ledger.mark_completed(
        stage=SUMMARIZATION_STAGE,
        input_artifacts=input_artifacts,
        output_artifacts=(ArtifactRef.from_record(old_record),),
        started_at=running.started_at,
        now=NOW,
    )
    provider = FakeSummarizationProvider(result=_summary_result())

    result = run_summarization_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        provider=provider,
        policy=SummarizationPolicy(language="de"),
        now=NOW,
    )

    assert result.skipped is False
    assert len(provider.requests) == 1
    assert result.summary_record.id != old_record.id
    assert result.summary.policy["prompt_policy"] == PROMPT_POLICY
    assert result.summary.provider.prompt_policy == PROMPT_POLICY
    assert len(store.list(artifact_type="summary")) == 2


def test_summarization_stage_reuse_ignores_manual_notes_when_policy_excludes_them() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(store=store)
    notes_record = _persist_notes(store=store)
    provider = FakeSummarizationProvider(result=_summary_result())
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)
    policy = SummarizationPolicy(include_manual_notes=False)

    first = run_summarization_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        provider=provider,
        policy=policy,
        now=NOW,
    )
    second = run_summarization_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        manual_notes_record=notes_record,
        provider=provider,
        policy=policy,
        now=NOW,
    )

    assert second.skipped is True
    assert second.summary_record == first.summary_record
    assert len(provider.requests) == 1


def test_summarization_stage_reuse_is_scoped_to_provider_config() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(store=store)
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    run_summarization_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        provider=FakeSummarizationProvider(result=_summary_result(), model="fake-a"),
        now=NOW,
    )
    second = run_summarization_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        provider=FakeSummarizationProvider(result=_summary_result(), model="fake-b"),
        attempt=2,
        now=NOW,
    )

    assert second.skipped is False


def test_summarization_stage_uses_distinct_output_paths_for_different_manual_notes() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(store=store)
    first_notes = _persist_notes(store=store)
    second_notes = store.store(
        artifact_type="manual_notes",
        name="other-notes.md",
        data=b"- Manual note: migration owner changed.",
        version=1,
        metadata={"contract": "manual-notes.raw"},
    )
    provider = FakeSummarizationProvider(result=_summary_result())
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    first = run_summarization_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        manual_notes_record=first_notes,
        provider=provider,
        now=NOW,
    )
    second = run_summarization_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        manual_notes_record=second_notes,
        provider=provider,
        now=NOW,
    )

    assert second.skipped is False
    assert first.summary_record.id != second.summary_record.id
    assert len(provider.requests) == 2


def test_summarization_stage_applies_correction_overlay_to_summary_request() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(store=store)
    overlay_record = _persist_correction_overlay(
        store=store,
        source_record=merged_record,
        entries=(
            CorrectionEntry(
                id="overlay_text",
                segment_id="merged_segment_000002",
                correction_type="text_edit",
                original_flag_id="flag_2",
                user_action_at="2026-05-18T17:00:01Z",
                text="Release is blocked unless dependencies ship.",
                metadata={},
            ),
        ),
        name="overlays/active.json",
    )
    provider = FakeSummarizationProvider(result=_summary_result())
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    result = run_summarization_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        correction_overlay_record=overlay_record,
        provider=provider,
        now=NOW,
    )

    assert result.summary_record is not None
    assert result.summary_record.metadata["correction_overlay_artifact_id"] == overlay_record.id
    corrected_segments = provider.requests[0].merged_transcript.segments
    assert corrected_segments[1].text == "Release is blocked unless dependencies ship."
    assert (
        "Release is blocked unless dependencies ship."
        in provider.requests[0].merged_transcript.text
    )


def test_summarization_stage_rejects_correction_overlay_with_source_mismatch() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(store=store)
    other_record = persist_merged_transcript_artifact(
        store=store,
        artifact=_merged_transcript_fixture(),
        name="merged/other.json",
    )
    overlay_record = _persist_correction_overlay(
        store=store,
        source_record=other_record,
        entries=(),
        name="overlays/mismatch.json",
    )
    provider = FakeSummarizationProvider(result=_summary_result())
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    with pytest.raises(SummarizationError, match="source record"):
        run_summarization_stage(
            store=store,
            ledger=ledger,
            merged_transcript_record=merged_record,
            correction_overlay_record=overlay_record,
            provider=provider,
            now=NOW,
        )


def test_summarization_stage_reuses_completed_output_when_overlay_is_stable() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(store=store)
    overlay_record = _persist_correction_overlay(
        store=store,
        source_record=merged_record,
        entries=(
            CorrectionEntry(
                id="overlay_reuse",
                segment_id="merged_segment_000002",
                correction_type="text_edit",
                original_flag_id="flag_2",
                user_action_at="2026-05-18T17:00:01Z",
                text="Release is blocked unless dependencies ship.",
                metadata={},
            ),
        ),
        name="overlays/reuse.json",
    )
    provider = FakeSummarizationProvider(result=_summary_result())
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    first = run_summarization_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        correction_overlay_record=overlay_record,
        provider=provider,
        now=NOW,
    )
    second = run_summarization_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        correction_overlay_record=overlay_record,
        provider=provider,
        now=NOW,
    )

    assert first.summary_record.metadata["correction_overlay_artifact_id"] == overlay_record.id
    assert second.summary_record.metadata["correction_overlay_artifact_id"] == overlay_record.id
    assert second.skipped is True
    assert second.summary_record == first.summary_record
    assert len(provider.requests) == 1


def test_summarization_stage_uses_distinct_paths_for_same_content_overlay_ids() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(store=store)
    entries = (
        CorrectionEntry(
            id="overlay_same_content",
            segment_id="merged_segment_000002",
            correction_type="text_edit",
            original_flag_id="flag_2",
            user_action_at="2026-05-18T17:00:01Z",
            text="Release is blocked unless dependencies ship.",
            metadata={},
        ),
    )
    first_overlay = _persist_correction_overlay(
        store=store,
        source_record=merged_record,
        entries=entries,
        name="overlays/same-content-a.json",
    )
    second_overlay = _persist_correction_overlay(
        store=store,
        source_record=merged_record,
        entries=entries,
        name="overlays/same-content-b.json",
    )
    provider = FakeSummarizationProvider(result=_summary_result())
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    first = run_summarization_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        correction_overlay_record=first_overlay,
        provider=provider,
        now=NOW,
    )
    second = run_summarization_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        correction_overlay_record=second_overlay,
        provider=provider,
        now=NOW,
    )

    assert first.summary_record.sha256 == second.summary_record.sha256
    assert first.summary_record.id != second.summary_record.id
    assert len(provider.requests) == 2


def test_summarization_stage_rejects_non_notes_artifact_when_notes_are_included() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(store=store)
    wrong_record = store.store(
        artifact_type="transcript",
        name="not-notes.txt",
        data=b"valid utf-8 but wrong artifact type",
        version=1,
        metadata={"contract": "transcript.v1"},
    )
    provider = FakeSummarizationProvider(result=_summary_result())
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    with pytest.raises(SummarizationError, match="manual_notes type"):
        run_summarization_stage(
            store=store,
            ledger=ledger,
            merged_transcript_record=merged_record,
            manual_notes_record=wrong_record,
            provider=provider,
            now=NOW,
        )


def test_summarization_stage_rejects_non_overlay_artifact_for_correction_overlay() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(store=store)
    wrong_record = store.store(
        artifact_type="transcript",
        name="not-overlay.txt",
        data=b"{}",
        version=1,
        metadata={"contract": "transcript.v1"},
    )
    provider = FakeSummarizationProvider(result=_summary_result())
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    with pytest.raises(SummarizationError, match="correction_overlay type"):
        run_summarization_stage(
            store=store,
            ledger=ledger,
            merged_transcript_record=merged_record,
            correction_overlay_record=wrong_record,
            provider=provider,
            now=NOW,
        )


def test_summarization_stage_marks_failed_checkpoint_for_missing_input_artifact() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(store=store)
    missing_record = replace(
        merged_record,
        id=f"{SESSION_ID}:artifacts/merged_transcript/missing.json",
        path="artifacts/merged_transcript/missing.json",
    )
    provider = FakeSummarizationProvider(result=_summary_result())
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    with pytest.raises(SummarizationError):
        run_summarization_stage(
            store=store,
            ledger=ledger,
            merged_transcript_record=missing_record,
            provider=provider,
            now=NOW,
        )

    failed = ledger.states(stage=SUMMARIZATION_STAGE)[-1]
    assert failed.status == "failed"
    assert failed.error is not None
    assert failed.error.code == "summarization_failed"


def test_summarization_stage_marks_failed_checkpoint_when_provider_unavailable() -> None:
    class UnavailableProvider:
        metadata = FakeSummarizationProvider(result=_summary_result()).metadata

        def summarize(self, request: object) -> SummaryResult:
            raise SummarizationProviderUnavailable("provider unavailable")

    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(store=store)
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    with pytest.raises(SummarizationProviderUnavailable, match="provider unavailable"):
        run_summarization_stage(
            store=store,
            ledger=ledger,
            merged_transcript_record=merged_record,
            provider=UnavailableProvider(),
            now=NOW,
        )

    failed = ledger.states(stage=SUMMARIZATION_STAGE)[-1]
    assert failed.status == "failed"
    assert failed.error is not None
    assert failed.error.retryable is True
    assert failed.error.code == "summarization_failed"


def test_summarization_stage_sanitizes_raw_provider_output_in_failed_checkpoint() -> None:
    class InvalidContractProvider:
        metadata = FakeSummarizationProvider(result=_summary_result()).metadata

        def summarize(self, request: object) -> SummaryResult:
            raise SummarizationContractError(
                "Ollama summary response was not valid JSON. "
                "Raw Ollama message content: confidential transcript text"
            )

    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(store=store)
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    with pytest.raises(SummarizationError) as exc_info:
        run_summarization_stage(
            store=store,
            ledger=ledger,
            merged_transcript_record=merged_record,
            provider=InvalidContractProvider(),
            now=NOW,
        )

    assert str(exc_info.value) == "Ollama summary response was not valid JSON."
    failed = ledger.states(stage=SUMMARIZATION_STAGE)[-1]
    assert failed.status == "failed"
    assert failed.error is not None
    assert failed.error.message == "Ollama summary response was not valid JSON."
    assert "confidential transcript text" not in failed.error.message


def _fake_product_responses(
    *, extra: dict[str, object] | None = None
) -> dict[str, dict[str, object]]:
    """Per-product Ollama response envelopes keyed by the schema's required field."""

    result = _summary_result()
    payloads: dict[str, dict[str, object]] = {
        "timeline": _timeline_provider_payload(result),
        "action_items": {"action_items": [action.to_dict() for action in result.action_items]},
        "general_summary": {"general_summary": result.general_summary},
    }
    return {
        product: {
            "message": {"content": json.dumps(payload)},
            "prompt_eval_count": 123,
            "eval_count": 45,
            **(extra or {}),
        }
        for product, payload in payloads.items()
    }


def _german_product_responses(
    *, general_summary: str = "Die Besprechung behandelte das Risiko der Datenbankmigration."
) -> dict[str, dict[str, object]]:
    """Per-product Ollama envelopes whose user-facing strings are in German."""

    payloads: dict[str, dict[str, object]] = {
        "timeline": {
            "timeline": [
                {
                    "text": "Diskussion über die Datenbankmigration vor Freitag.",
                    "source_segment_ids": ["merged_segment_000001"],
                }
            ]
        },
        "action_items": {
            "action_items": [
                {"text": "Datenbankmigration bis Freitag abschließen.", "owner": "Du", "due": None}
            ]
        },
        "general_summary": {"general_summary": general_summary},
    }
    return {
        product: {
            "message": {"content": json.dumps(payload, ensure_ascii=False)},
            "prompt_eval_count": 123,
            "eval_count": 45,
        }
        for product, payload in payloads.items()
    }


def _product_key(payload: dict[str, object]) -> str:
    schema = cast(dict[str, object], payload["format"])
    return cast(list[str], schema["required"])[0]


def _timeline_provider_payload(result: SummaryResult) -> dict[str, object]:
    return {"timeline": [entry.to_provider_dict() for entry in result.timeline]}


def test_ollama_summarization_provider_runs_one_shot_per_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSummarizationProvider(model="test-model")
    responses = _fake_product_responses()
    seen: list[str] = []

    def fake_post_json(path: str, payload: object) -> dict[str, object]:
        assert path == "/api/chat"
        assert isinstance(payload, dict)
        assert payload["model"] == "test-model"
        assert payload["stream"] is False
        assert payload["options"] == {
            "temperature": 0,
            "seed": OLLAMA_SUMMARIZATION_SEED,
            "num_ctx": OLLAMA_SUMMARIZATION_NUM_CTX,
        }
        schema = cast(dict[str, object], payload["format"])
        assert schema["additionalProperties"] is False
        product = _product_key(payload)
        seen.append(product)
        messages = cast(list[dict[str, str]], payload["messages"])
        assert "meeting_notes" in messages[1]["content"]
        return responses[product]

    monkeypatch.setattr(provider, "_post_json", fake_post_json)

    result = provider.summarize(_summary_request())

    assert seen == ["timeline", "action_items", "general_summary"]
    assert result.timeline[0].text == "Discussed database migration before Friday."
    assert result.action_items[0].owner == "You"
    assert result.general_summary == "The meeting focused on database migration risk."


def test_ollama_summarization_provider_records_request_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSummarizationProvider(model="test-model")
    responses = _fake_product_responses(extra={"prompt_eval_count": 321, "eval_count": 42})

    def fake_post_json(path: str, payload: object) -> dict[str, object]:
        assert isinstance(payload, dict)
        return responses[_product_key(payload)]

    monkeypatch.setattr(provider, "_post_json", fake_post_json)

    result = provider.summarize(_summary_request())

    assert [record.product for record in result.request_records] == [
        "timeline",
        "action_items",
        "general_summary",
    ]
    for record in result.request_records:
        assert record.model == "test-model"
        assert record.num_ctx == OLLAMA_SUMMARIZATION_NUM_CTX
        assert record.temperature == 0
        assert record.seed == OLLAMA_SUMMARIZATION_SEED
        assert record.prompt_eval_count == 321
        assert record.eval_count == 42


@pytest.mark.parametrize(
    ("missing_count", "present_count"),
    [
        ("prompt_eval_count", "eval_count"),
        ("eval_count", "prompt_eval_count"),
    ],
)
def test_ollama_summarization_provider_rejects_missing_token_counts(
    monkeypatch: pytest.MonkeyPatch,
    missing_count: str,
    present_count: str,
) -> None:
    provider = OllamaSummarizationProvider(model="test-model")
    responses = _fake_product_responses(extra={missing_count: None, present_count: 1})

    def fake_post_json(path: str, payload: object) -> dict[str, object]:
        assert isinstance(payload, dict)
        return responses[_product_key(payload)]

    monkeypatch.setattr(provider, "_post_json", fake_post_json)

    with pytest.raises(
        SummarizationContractError,
        match=f"omitted required token counts: {missing_count}",
    ):
        provider.summarize(_summary_request())


def test_ollama_summarization_provider_rejects_short_word_prompt_over_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSummarizationProvider(model="test-model")
    merged_transcript = _summary_request().merged_transcript
    request = replace(
        _summary_request(),
        merged_transcript=replace(
            merged_transcript,
            segments=(
                replace(
                    merged_transcript.segments[0],
                    text=" ".join(["a"] * OLLAMA_SUMMARIZATION_NUM_CTX),
                ),
            ),
        ),
    )

    def fail_if_called(_path: str, _payload: object) -> dict[str, object]:
        raise AssertionError("oversized prompt should fail before provider call")

    monkeypatch.setattr(provider, "_post_json", fail_if_called)

    with pytest.raises(SummarizationTokenBudgetError, match="exceed the configured"):
        provider.summarize(request)


def test_ollama_summarization_provider_instructs_non_english_output_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSummarizationProvider(model="test-model")
    responses = _german_product_responses()

    def fake_post_json(path: str, payload: object) -> dict[str, object]:
        assert isinstance(payload, dict)
        messages = cast(list[dict[str, str]], payload["messages"])
        assert "Write every user-facing string in de." in messages[0]["content"]
        user_payload = json.loads(messages[1]["content"])
        assert user_payload["output_language"] == (
            "Write all user-facing strings in de: timeline entries, "
            "action item text, generated owner/due wording, and the general summary."
        )
        return responses[_product_key(payload)]

    monkeypatch.setattr(provider, "_post_json", fake_post_json)

    request = replace(_summary_request(), policy=SummarizationPolicy(language="de"))
    result = provider.summarize(request)

    assert result.general_summary == "Die Besprechung behandelte das Risiko der Datenbankmigration."


def test_ollama_summarization_provider_hard_fails_on_english_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSummarizationProvider(model="test-model")
    responses = _fake_product_responses()  # English content for a German request.
    responses["general_summary"]["message"] = {
        "content": json.dumps(
            {
                "general_summary": (
                    "The team is working with the database and the migration before Friday."
                )
            }
        )
    }

    def fake_post_json(path: str, payload: object) -> dict[str, object]:
        assert isinstance(payload, dict)
        return responses[_product_key(payload)]

    monkeypatch.setattr(provider, "_post_json", fake_post_json)

    request = replace(_summary_request(), policy=SummarizationPolicy(language="de"))
    with pytest.raises(SummarizationContractError, match="drifted to English"):
        provider.summarize(request)


def test_ollama_summarization_provider_hard_fails_on_per_field_english_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSummarizationProvider(model="test-model")
    responses = _german_product_responses()
    responses["action_items"]["message"] = {
        "content": json.dumps(
            {
                "action_items": [
                    {
                        "text": "The team should finish the migration before Friday.",
                        "owner": "Du",
                        "due": None,
                    }
                ]
            }
        )
    }

    def fake_post_json(path: str, payload: object) -> dict[str, object]:
        assert isinstance(payload, dict)
        return responses[_product_key(payload)]

    monkeypatch.setattr(provider, "_post_json", fake_post_json)

    request = replace(_summary_request(), policy=SummarizationPolicy(language="de"))
    with pytest.raises(
        SummarizationContractError,
        match=r"action_items\[0\]\.text output drifted to English",
    ):
        provider.summarize(request)


def test_ollama_summarization_provider_accepts_german_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSummarizationProvider(model="test-model")
    responses = _german_product_responses()

    def fake_post_json(path: str, payload: object) -> dict[str, object]:
        assert isinstance(payload, dict)
        return responses[_product_key(payload)]

    monkeypatch.setattr(provider, "_post_json", fake_post_json)

    request = replace(_summary_request(), policy=SummarizationPolicy(language="de-CH"))
    result = provider.summarize(request)

    assert result.timeline[0].text.startswith("Diskussion")


def test_ollama_summarization_provider_accepts_german_output_with_english_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A few embedded English technical nouns must not trip the drift guard.
    provider = OllamaSummarizationProvider(model="test-model")
    responses = _german_product_responses(
        general_summary="Das Team migriert den Cloud Service und den API Gateway bis Freitag."
    )

    def fake_post_json(path: str, payload: object) -> dict[str, object]:
        assert isinstance(payload, dict)
        return responses[_product_key(payload)]

    monkeypatch.setattr(provider, "_post_json", fake_post_json)

    request = replace(_summary_request(), policy=SummarizationPolicy(language="de"))
    result = provider.summarize(request)

    assert "Cloud Service" in result.general_summary


def test_ollama_summarization_provider_hard_fails_over_token_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSummarizationProvider(model="test-model")
    calls: list[str] = []

    def fake_post_json(path: str, payload: object) -> dict[str, object]:
        calls.append(path)
        return {"message": {"content": "{}"}}

    monkeypatch.setattr(provider, "_post_json", fake_post_json)
    oversized = "word " * (OLLAMA_SUMMARIZATION_NUM_CTX * 4)
    request = replace(_summary_request(), manual_notes=oversized)

    with pytest.raises(SummarizationTokenBudgetError, match="num_ctx budget"):
        provider.summarize(request)

    assert calls == []


def test_ollama_summarization_provider_rejects_invalid_json_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSummarizationProvider(model="test-model")

    def fake_post_json(path: str, payload: object) -> dict[str, object]:
        return {"message": {"content": "not-json"}}

    monkeypatch.setattr(provider, "_post_json", fake_post_json)

    with pytest.raises(SummarizationContractError) as exc_info:
        provider.summarize(_summary_request())

    message = str(exc_info.value)
    assert "timeline response was not valid JSON" in message
    assert "Raw Ollama message content: not-json" in message


def test_ollama_summarization_provider_rejects_length_truncated_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSummarizationProvider(model="test-model")
    responses = _fake_product_responses(extra={"done_reason": "length"})

    def fake_post_json(path: str, payload: object) -> dict[str, object]:
        assert isinstance(payload, dict)
        return responses[_product_key(payload)]

    monkeypatch.setattr(provider, "_post_json", fake_post_json)

    with pytest.raises(SummarizationContractError) as exc_info:
        provider.summarize(_summary_request())

    message = str(exc_info.value)
    assert "Ollama timeline response was truncated" in message
    assert "done_reason=length" in message
    assert "Raw Ollama message content:" in message


def test_ollama_summarization_provider_preserves_raw_output_on_contract_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSummarizationProvider(model="test-model")
    responses = _fake_product_responses()
    raw_content = json.dumps({"general_summary": None})
    responses["general_summary"] = {
        "message": {"content": raw_content},
        "prompt_eval_count": 123,
        "eval_count": 45,
    }

    def fake_post_json(path: str, payload: object) -> dict[str, object]:
        assert isinstance(payload, dict)
        return responses[_product_key(payload)]

    monkeypatch.setattr(provider, "_post_json", fake_post_json)

    with pytest.raises(SummarizationContractError) as exc_info:
        provider.summarize(_summary_request())

    message = str(exc_info.value)
    assert "Field 'general_summary' in Summary result must be a non-empty string." in message
    assert "Raw Ollama message content:" in message
    assert raw_content in message


def _long_merged_transcript_fixture(*, segment_count: int = 60) -> MergedTranscriptArtifact:
    """Synthetic long transcript that exceeds the one-shot prompt budget.

    Content is generated filler so the fixture stays free of private meeting
    material while still forcing the reducer route by size.
    """

    base = _merged_transcript_fixture()
    template = base.segments[0]
    segments = tuple(
        replace(
            template,
            id=f"merged_segment_{index:06d}",
            text=(
                f"Segment {index}: the team reviewed synthetic agenda topic {index}, "
                "compared the available deployment options in detail, and recorded "
                "the follow-up questions that remained open for the next milestone."
            ),
            start_seconds=float(index),
            end_seconds=float(index) + 0.9,
            transcript_segment_id=f"local_segment_{index}",
        )
        for index in range(segment_count)
    )
    return replace(base, segments=segments, text=" ".join(segment.text for segment in segments))


def _reducer_map_responses() -> dict[str, dict[str, object]]:
    payloads: dict[str, dict[str, object]] = {
        "timeline": {
            "timeline": [
                {
                    "text": "Reviewed one synthetic agenda topic.",
                    "source_segment_ids": ["merged_segment_000000"],
                }
            ]
        },
        "action_items": {
            "action_items": [
                {"text": "Answer the open follow-up questions.", "owner": None, "due": None}
            ]
        },
        "general_summary": {"general_summary": "This chunk covered one agenda topic."},
    }
    return {
        product: {
            "message": {"content": json.dumps(payload)},
            "prompt_eval_count": 222,
            "eval_count": 33,
        }
        for product, payload in payloads.items()
    }


def _strategy_aware_post_json(
    *,
    reduce_responses: dict[str, dict[str, object]],
    map_responses: dict[str, dict[str, object]],
    calls: list[dict[str, object]],
) -> "object":
    def fake_post_json(path: str, payload: object) -> dict[str, object]:
        assert path == "/api/chat"
        assert isinstance(payload, dict)
        product = _product_key(payload)
        messages = cast(list[dict[str, str]], payload["messages"])
        user_payload = json.loads(messages[1]["content"])
        calls.append(
            {
                "product": product,
                "task": user_payload["task"],
                "chunk_index": user_payload.get("chunk_index"),
                "chunk_count": user_payload.get("chunk_count"),
                "has_manual_notes": "manual_notes" in user_payload,
                "system_content": messages[0]["content"],
            }
        )
        if user_payload["task"] == "meeting_notes_chunk":
            response = json.loads(json.dumps(map_responses[product]))
            if product == "timeline":
                response_payload = json.loads(response["message"]["content"])
                transcript = cast(dict[str, object], user_payload["transcript"])
                segments = cast(list[dict[str, object]], transcript["segments"])
                first_segment_id = segments[0]["id"]
                for entry in response_payload["timeline"]:
                    if isinstance(entry, dict) and entry.get("source_segment_ids") == [
                        "merged_segment_000000"
                    ]:
                        entry["source_segment_ids"] = [first_segment_id]
                response["message"]["content"] = json.dumps(response_payload)
            return cast(dict[str, object], response)
        return reduce_responses[product]

    return fake_post_json


def _long_summary_request() -> SummarizationRequest:
    return replace(_summary_request(), merged_transcript=_long_merged_transcript_fixture())


def test_ollama_provider_routes_long_input_to_reducer_per_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSummarizationProvider(model="test-model")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        provider,
        "_post_json",
        _strategy_aware_post_json(
            reduce_responses=_fake_product_responses(),
            map_responses=_reducer_map_responses(),
            calls=calls,
        ),
    )

    result = provider.summarize(_long_summary_request())

    assert {call["task"] for call in calls} == {"meeting_notes_chunk", "meeting_notes_reduce"}
    for product in REQUIRED_NOTE_PRODUCTS:
        product_calls = [call for call in calls if call["product"] == product]
        map_calls = [call for call in product_calls if call["task"] == "meeting_notes_chunk"]
        reduce_calls = [call for call in product_calls if call["task"] == "meeting_notes_reduce"]
        assert len(map_calls) >= 2
        assert len(reduce_calls) == 1
        assert [call["chunk_index"] for call in map_calls] == list(range(len(map_calls)))
        assert all(call["chunk_count"] == len(map_calls) for call in map_calls)
        # Manual notes are global context: reduce-only, never per chunk.
        assert all(call["has_manual_notes"] is False for call in map_calls)
        assert reduce_calls[0]["has_manual_notes"] is True
        # Official Ollama guidance: the schema is also passed in the prompt.
        assert all(
            "satisfies this JSON schema" in cast(str, call["system_content"])
            for call in product_calls
        )
    assert result.timeline[0].text == "Discussed database migration before Friday."
    assert result.general_summary == "The meeting focused on database migration risk."


def test_ollama_provider_records_per_call_reducer_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSummarizationProvider(model="test-model")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        provider,
        "_post_json",
        _strategy_aware_post_json(
            reduce_responses=_fake_product_responses(),
            map_responses=_reducer_map_responses(),
            calls=calls,
        ),
    )

    result = provider.summarize(_long_summary_request())

    for product in REQUIRED_NOTE_PRODUCTS:
        records = [record for record in result.request_records if record.product == product]
        map_records = [record for record in records if record.call == "map"]
        reduce_records = [record for record in records if record.call == "reduce"]
        assert all(record.strategy == "reducer" for record in records)
        assert len(map_records) >= 2
        assert len(reduce_records) == 1
        assert [record.chunk_index for record in map_records] == list(range(len(map_records)))
        assert all(record.chunk_count == len(map_records) for record in map_records)
        assert reduce_records[0].chunk_index is None
        assert reduce_records[0].chunk_count == len(map_records)
        assert all(record.prompt_eval_count == 222 for record in map_records)
        assert all(record.eval_count == 33 for record in map_records)
        assert reduce_records[0].prompt_eval_count == 123
        assert reduce_records[0].eval_count == 45
        assert all(record.num_ctx == OLLAMA_SUMMARIZATION_NUM_CTX for record in records)


def test_ollama_provider_short_input_records_one_shot_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSummarizationProvider(model="test-model")
    responses = _fake_product_responses()
    calls: list[str] = []

    def fake_post_json(path: str, payload: object) -> dict[str, object]:
        assert isinstance(payload, dict)
        product = _product_key(payload)
        calls.append(product)
        messages = cast(list[dict[str, str]], payload["messages"])
        assert json.loads(messages[1]["content"])["task"] == "meeting_notes"
        return responses[product]

    monkeypatch.setattr(provider, "_post_json", fake_post_json)

    result = provider.summarize(_summary_request())

    assert calls == list(REQUIRED_NOTE_PRODUCTS)
    assert [record.strategy for record in result.request_records] == ["one_shot"] * 3
    assert [record.call for record in result.request_records] == ["one_shot"] * 3
    assert all(record.chunk_index is None for record in result.request_records)
    assert all(record.chunk_count is None for record in result.request_records)


def test_ollama_provider_reducer_fails_loudly_when_reduce_candidates_exceed_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSummarizationProvider(model="test-model")
    oversized_map_responses = _reducer_map_responses()
    oversized_map_responses["timeline"] = {
        "message": {
            "content": json.dumps(
                {
                    "timeline": [
                        {
                            "text": "An oversized event entry. " * 400,
                            "source_segment_ids": ["merged_segment_000000"],
                        }
                    ]
                }
            )
        },
        "prompt_eval_count": 222,
        "eval_count": 33,
    }
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        provider,
        "_post_json",
        _strategy_aware_post_json(
            reduce_responses=_fake_product_responses(),
            map_responses=oversized_map_responses,
            calls=calls,
        ),
    )

    with pytest.raises(
        SummarizationTokenBudgetError,
        match="timeline reduce prompt tokens .* exceed the configured",
    ):
        provider.summarize(_long_summary_request())

    assert all(call["task"] == "meeting_notes_chunk" for call in calls)


def test_ollama_provider_reducer_fails_loudly_before_map_calls_when_notes_never_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSummarizationProvider(model="test-model")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        provider,
        "_post_json",
        _strategy_aware_post_json(
            reduce_responses=_fake_product_responses(),
            map_responses=_reducer_map_responses(),
            calls=calls,
        ),
    )
    request = replace(
        _long_summary_request(),
        manual_notes="An oversized manual note. " * (OLLAMA_SUMMARIZATION_NUM_CTX // 2),
    )

    with pytest.raises(
        SummarizationTokenBudgetError,
        match="before any chunk candidates",
    ):
        provider.summarize(request)

    assert calls == []


@pytest.mark.parametrize("manual_notes", [None, "   \n\t"])
def test_ollama_provider_reducer_rejects_empty_timeline_candidates_without_notes(
    monkeypatch: pytest.MonkeyPatch,
    manual_notes: str | None,
) -> None:
    # A whitespace-only notes file is ingested as a truthy string but carries
    # no grounding facts; it must not bypass the empty-candidate guard.
    provider = OllamaSummarizationProvider(model="test-model")
    empty_map_responses = _reducer_map_responses()
    empty_map_responses["timeline"] = {
        "message": {"content": json.dumps({"timeline": []})},
        "prompt_eval_count": 222,
        "eval_count": 33,
    }
    monkeypatch.setattr(
        provider,
        "_post_json",
        _strategy_aware_post_json(
            reduce_responses=_fake_product_responses(),
            map_responses=empty_map_responses,
            calls=[],
        ),
    )
    request = replace(_long_summary_request(), manual_notes=manual_notes)

    with pytest.raises(
        SummarizationContractError,
        match="no manual notes are available; refusing to reduce an empty timeline",
    ):
        provider.summarize(request)


def test_ollama_provider_reducer_allows_manual_notes_to_ground_empty_timeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Strategy selection must stay invisible in acceptance behavior (ADR-035):
    # the one-shot path generates notes-informed timelines, so the reducer must
    # let manual notes ground the reduce call when every chunk is empty.
    provider = OllamaSummarizationProvider(model="test-model")
    empty_map_responses = _reducer_map_responses()
    empty_map_responses["timeline"] = {
        "message": {"content": json.dumps({"timeline": []})},
        "prompt_eval_count": 222,
        "eval_count": 33,
    }
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        provider,
        "_post_json",
        _strategy_aware_post_json(
            reduce_responses=_fake_product_responses(),
            map_responses=empty_map_responses,
            calls=calls,
        ),
    )

    result = provider.summarize(_long_summary_request())

    timeline_reduce_calls = [
        call
        for call in calls
        if call["product"] == "timeline" and call["task"] == "meeting_notes_reduce"
    ]
    assert len(timeline_reduce_calls) == 1
    assert timeline_reduce_calls[0]["has_manual_notes"] is True
    assert result.timeline[0].text == "Discussed database migration before Friday."


def test_ollama_provider_reducer_rejects_invalid_map_payload_with_raw_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSummarizationProvider(model="test-model")
    invalid_map_responses = _reducer_map_responses()
    raw_content = json.dumps({"timeline": [123]})
    invalid_map_responses["timeline"] = {
        "message": {"content": raw_content},
        "prompt_eval_count": 222,
        "eval_count": 33,
    }
    monkeypatch.setattr(
        provider,
        "_post_json",
        _strategy_aware_post_json(
            reduce_responses=_fake_product_responses(),
            map_responses=invalid_map_responses,
            calls=[],
        ),
    )

    with pytest.raises(SummarizationContractError) as exc_info:
        provider.summarize(_long_summary_request())

    message = str(exc_info.value)
    assert "Summary timeline chunk candidates[0] must be a JSON object." in message
    assert raw_content in message


def test_ollama_provider_reducer_rejects_timeline_map_source_outside_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSummarizationProvider(model="test-model")
    invalid_map_responses = _reducer_map_responses()
    invalid_map_responses["timeline"] = {
        "message": {
            "content": json.dumps(
                {
                    "timeline": [
                        {
                            "text": "Cited a segment from a different chunk.",
                            "source_segment_ids": ["merged_segment_000059"],
                        }
                    ]
                }
            )
        },
        "prompt_eval_count": 222,
        "eval_count": 33,
    }
    monkeypatch.setattr(
        provider,
        "_post_json",
        _strategy_aware_post_json(
            reduce_responses=_fake_product_responses(),
            map_responses=invalid_map_responses,
            calls=[],
        ),
    )

    with pytest.raises(SummarizationContractError) as exc_info:
        provider.summarize(_long_summary_request())

    assert "out-of-chunk ids: merged_segment_000059" in str(exc_info.value)


def test_summarization_stage_persists_reducer_provenance_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = persist_merged_transcript_artifact(
        store=store,
        artifact=_long_merged_transcript_fixture(),
        name="merged-long.json",
    )
    provider = OllamaSummarizationProvider(model="test-model")
    monkeypatch.setattr(
        provider,
        "_post_json",
        _strategy_aware_post_json(
            reduce_responses=_fake_product_responses(),
            map_responses=_reducer_map_responses(),
            calls=[],
        ),
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    result = run_summarization_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        provider=provider,
        now=NOW,
    )

    payload = json.loads(store.retrieve(result.summary_record.id))
    Draft202012Validator(_load_summary_schema(), format_checker=FormatChecker()).validate(payload)
    assert result.summary_record.metadata["note_product_strategies"] == (
        "timeline=reducer,action_items=reducer,general_summary=reducer"
    )
    reloaded = SummaryArtifact.from_dict(payload)
    assert {record.strategy for record in reloaded.request_records} == {"reducer"}
    assert len(reloaded.request_records) > len(REQUIRED_NOTE_PRODUCTS)


def test_summarization_policy_and_result_validate_contract() -> None:
    assert "provider_config_id" not in SummarizationPolicy(language="en").to_dict()
    assert SummarizationPolicy(language="de_CH").language == "de-CH"

    with pytest.raises(SummarizationContractError, match="Unsupported summarization prompt"):
        SummarizationPolicy.from_dict(
            {
                "policy_version": 1,
                "prompt_policy": "other",
                "language": "en",
                "include_manual_notes": True,
            }
        )

    with pytest.raises(SummarizationContractError, match="timeline must not be empty"):
        SummaryResult.from_dict(
            {
                "timeline": [],
                "action_items": [],
                "general_summary": "Summary",
            }
        )


def test_summary_artifact_from_dict_validates_contract() -> None:
    artifact = SummaryArtifact(
        version=SUMMARY_SCHEMA_VERSION,
        session_id=SESSION_ID,
        merged_transcript_artifact=ArtifactRef(
            id=f"{SESSION_ID}:artifacts/merged_transcript/merged.json",
            artifact_type="merged_transcript",
            version=1,
            sha256="1" * 64,
        ),
        manual_notes_artifact=None,
        policy=SummarizationPolicy(language="en").to_dict(),
        provider=FakeSummarizationProvider(result=_summary_result()).metadata,
        timeline=_summary_result().timeline,
        action_items=_summary_result().action_items,
        general_summary=_summary_result().general_summary,
        created_at="2026-05-18T17:00:00Z",
        request_records=_summary_result().request_records,
    )
    payload = artifact.to_dict()
    payload["version"] = 99

    with pytest.raises(SummarizationContractError, match="Unsupported summary"):
        SummaryArtifact.from_dict(payload)

    payload = artifact.to_dict()
    cast(dict[str, object], payload["provider"])["prompt_policy"] = "other"
    with pytest.raises(SummarizationContractError, match="Unsupported summary provider"):
        SummaryArtifact.from_dict(payload)


def test_summary_artifact_from_dict_requires_request_records() -> None:
    artifact = SummaryArtifact(
        version=SUMMARY_SCHEMA_VERSION,
        session_id=SESSION_ID,
        merged_transcript_artifact=ArtifactRef(
            id=f"{SESSION_ID}:artifacts/merged_transcript/merged.json",
            artifact_type="merged_transcript",
            version=1,
            sha256="1" * 64,
        ),
        manual_notes_artifact=None,
        policy=SummarizationPolicy(language="en").to_dict(),
        provider=FakeSummarizationProvider(result=_summary_result()).metadata,
        timeline=_summary_result().timeline,
        action_items=_summary_result().action_items,
        general_summary=_summary_result().general_summary,
        created_at="2026-05-18T17:00:00Z",
        request_records=_summary_result().request_records,
    )

    payload = artifact.to_dict()
    del payload["request_records"]
    with pytest.raises(SummarizationContractError, match="request_records' is required"):
        SummaryArtifact.from_dict(payload)

    payload = artifact.to_dict()
    payload["request_records"] = None
    with pytest.raises(SummarizationContractError, match="request_records' is required"):
        SummaryArtifact.from_dict(payload)


@pytest.mark.parametrize(
    ("records", "message"),
    [
        ((), "at least one record for timeline"),
        (
            (
                "timeline",
                "action_items",
            ),
            "at least one record for general_summary",
        ),
        (
            (
                "timeline",
                "timeline",
                "general_summary",
            ),
            "exactly one one_shot record for timeline",
        ),
        (
            (
                "timeline",
                "action_items",
                "general_summary",
                "other",
            ),
            "must only contain records for the note",
        ),
    ],
)
def test_summary_artifact_from_dict_requires_complete_request_records(
    records: tuple[str, ...],
    message: str,
) -> None:
    artifact = SummaryArtifact(
        version=SUMMARY_SCHEMA_VERSION,
        session_id=SESSION_ID,
        merged_transcript_artifact=ArtifactRef(
            id=f"{SESSION_ID}:artifacts/merged_transcript/merged.json",
            artifact_type="merged_transcript",
            version=1,
            sha256="1" * 64,
        ),
        manual_notes_artifact=None,
        policy=SummarizationPolicy(language="en").to_dict(),
        provider=FakeSummarizationProvider(result=_summary_result()).metadata,
        timeline=_summary_result().timeline,
        action_items=_summary_result().action_items,
        general_summary=_summary_result().general_summary,
        created_at="2026-05-18T17:00:00Z",
        request_records=_request_records_for_products(records),
    )

    with pytest.raises(SummarizationContractError, match=message):
        SummaryArtifact.from_dict(artifact.to_dict())


def _summary_artifact_with_records(
    records: tuple[ProductRequestRecord, ...],
) -> SummaryArtifact:
    result = _summary_result()
    return SummaryArtifact(
        version=SUMMARY_SCHEMA_VERSION,
        session_id=SESSION_ID,
        merged_transcript_artifact=ArtifactRef(
            id=f"{SESSION_ID}:artifacts/merged_transcript/merged.json",
            artifact_type="merged_transcript",
            version=1,
            sha256="1" * 64,
        ),
        manual_notes_artifact=None,
        policy=SummarizationPolicy(language="en").to_dict(),
        provider=FakeSummarizationProvider(result=result).metadata,
        timeline=result.timeline,
        action_items=result.action_items,
        general_summary=result.general_summary,
        created_at="2026-05-18T17:00:00Z",
        request_records=records,
    )


def _reducer_records_for_product(
    product: str,
    *,
    chunk_count: int = 2,
) -> tuple[ProductRequestRecord, ...]:
    map_records = tuple(
        ProductRequestRecord(
            product=product,
            model="fake-summarizer",
            num_ctx=OLLAMA_SUMMARIZATION_NUM_CTX,
            temperature=0,
            seed=OLLAMA_SUMMARIZATION_SEED,
            prompt_eval_count=100,
            eval_count=20,
            strategy="reducer",
            call="map",
            chunk_index=index,
            chunk_count=chunk_count,
        )
        for index in range(chunk_count)
    )
    reduce_record = ProductRequestRecord(
        product=product,
        model="fake-summarizer",
        num_ctx=OLLAMA_SUMMARIZATION_NUM_CTX,
        temperature=0,
        seed=OLLAMA_SUMMARIZATION_SEED,
        prompt_eval_count=60,
        eval_count=40,
        strategy="reducer",
        call="reduce",
        chunk_index=None,
        chunk_count=chunk_count,
    )
    return (*map_records, reduce_record)


def test_summary_artifact_accepts_mixed_one_shot_and_reducer_products() -> None:
    records = (
        *_reducer_records_for_product("timeline"),
        *_request_records_for_products(("action_items", "general_summary")),
    )
    artifact = _summary_artifact_with_records(records)

    loaded = SummaryArtifact.from_dict(artifact.to_dict())

    assert [record.call for record in loaded.request_records if record.product == "timeline"] == [
        "map",
        "map",
        "reduce",
    ]
    Draft202012Validator(_load_summary_schema(), format_checker=FormatChecker()).validate(
        artifact.to_dict()
    )


def test_summary_artifact_rejects_mixed_strategies_within_one_product() -> None:
    records = (
        *_reducer_records_for_product("timeline"),
        *_request_records_for_products(("timeline", "action_items", "general_summary")),
    )

    with pytest.raises(SummarizationContractError, match="one strategy for timeline"):
        SummaryArtifact.from_dict(_summary_artifact_with_records(records).to_dict())


def test_summary_artifact_rejects_reducer_product_without_reduce_call() -> None:
    reducer_records = _reducer_records_for_product("timeline")
    records = (
        *(record for record in reducer_records if record.call == "map"),
        *_request_records_for_products(("action_items", "general_summary")),
    )

    with pytest.raises(SummarizationContractError, match="exactly one reduce call"):
        SummaryArtifact.from_dict(_summary_artifact_with_records(records).to_dict())


def test_summary_artifact_rejects_reducer_map_calls_with_chunk_gaps() -> None:
    reducer_records = _reducer_records_for_product("timeline", chunk_count=3)
    records = (
        *(record for record in reducer_records if record.chunk_index != 1),
        *_request_records_for_products(("action_items", "general_summary")),
    )

    with pytest.raises(SummarizationContractError, match="cover each chunk index exactly once"):
        SummaryArtifact.from_dict(_summary_artifact_with_records(records).to_dict())


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"strategy": "chunked"}, "strategy must be one_shot or reducer"),
        ({"call": "shuffle"}, "call must be one_shot, map, or reduce"),
        ({"call": "map"}, "strategy and call kind must agree"),
        ({"chunk_index": 0}, "one_shot calls must not set chunk fields"),
        (
            {"strategy": "reducer", "call": "map"},
            "map calls require chunk_index and chunk_count",
        ),
        (
            {"strategy": "reducer", "call": "map", "chunk_index": 2, "chunk_count": 2},
            "chunk_index must be within chunk_count",
        ),
        (
            {"strategy": "reducer", "call": "reduce", "chunk_index": 0, "chunk_count": 1},
            "reduce calls must not set chunk_index",
        ),
        (
            {"strategy": "reducer", "call": "reduce"},
            "reduce calls require chunk_count",
        ),
    ],
)
def test_product_request_record_rejects_incoherent_strategy_fields(
    overrides: dict[str, object],
    message: str,
) -> None:
    base = _request_records_for_products(("timeline",))[0].to_dict()
    base.update(overrides)

    with pytest.raises(SummarizationContractError, match=message):
        ProductRequestRecord.from_dict(base)


@pytest.mark.parametrize("missing_field", ["chunk_index", "chunk_count"])
def test_product_request_record_requires_explicit_nullable_chunk_fields(
    missing_field: str,
) -> None:
    payload = _request_records_for_products(("timeline",))[0].to_dict()
    payload.pop(missing_field)

    with pytest.raises(
        SummarizationContractError, match="must include chunk_index and chunk_count"
    ):
        ProductRequestRecord.from_dict(payload)


def test_persist_summary_artifact_records_note_product_strategies_metadata() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    records = (
        *_reducer_records_for_product("timeline"),
        *_request_records_for_products(("action_items", "general_summary")),
    )
    artifact = _summary_artifact_with_records(records)

    record = persist_summary_artifact(
        store=store,
        artifact=artifact,
        name="summary/strategies.json",
        correction_overlay_record=None,
    )

    assert record.metadata["note_product_strategies"] == (
        "timeline=reducer,action_items=one_shot,general_summary=one_shot"
    )


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("num_ctx", 0, "at least 1"),
        ("prompt_eval_count", -1, "at least 0"),
        ("eval_count", -1, "at least 0"),
    ],
)
def test_summary_artifact_from_dict_rejects_out_of_range_request_record_metrics(
    field_name: str,
    value: int,
    message: str,
) -> None:
    artifact = SummaryArtifact(
        version=SUMMARY_SCHEMA_VERSION,
        session_id=SESSION_ID,
        merged_transcript_artifact=ArtifactRef(
            id=f"{SESSION_ID}:artifacts/merged_transcript/merged.json",
            artifact_type="merged_transcript",
            version=1,
            sha256="1" * 64,
        ),
        manual_notes_artifact=None,
        policy=SummarizationPolicy(language="en").to_dict(),
        provider=FakeSummarizationProvider(result=_summary_result()).metadata,
        timeline=_summary_result().timeline,
        action_items=_summary_result().action_items,
        general_summary=_summary_result().general_summary,
        created_at="2026-05-18T17:00:00Z",
        request_records=_summary_result().request_records,
    )
    payload = artifact.to_dict()
    request_records = cast(list[dict[str, object]], payload["request_records"])
    request_records[0][field_name] = value

    with pytest.raises(SummarizationContractError, match=message):
        SummaryArtifact.from_dict(payload)


def test_provider_request_options_participate_in_reuse_identity() -> None:
    base = SummarizationProviderMetadata(name="ollama", model="llama3.1:8b", endpoint=None)
    tuned = SummarizationProviderMetadata(
        name="ollama",
        model="llama3.1:8b",
        endpoint=None,
        num_ctx=OLLAMA_SUMMARIZATION_NUM_CTX,
        temperature=0,
        seed=OLLAMA_SUMMARIZATION_SEED,
    )
    retuned_seed = replace(tuned, seed=OLLAMA_SUMMARIZATION_SEED + 1)
    retuned_ctx = replace(tuned, num_ctx=OLLAMA_SUMMARIZATION_NUM_CTX * 2)

    # Providers without request options keep the pre-existing reuse identity.
    assert (
        base.config_id
        == SummarizationProviderMetadata(
            name="ollama", model="llama3.1:8b", endpoint=None
        ).config_id
    )
    # Applying request discipline changes the reuse identity.
    assert tuned.config_id != base.config_id
    # Retuning any request-discipline option invalidates the reuse identity.
    assert retuned_seed.config_id != tuned.config_id
    assert retuned_ctx.config_id != tuned.config_id


def test_summary_artifact_from_dict_accepts_legacy_prompt_policy() -> None:
    artifact = SummaryArtifact(
        version=SUMMARY_SCHEMA_VERSION,
        session_id=SESSION_ID,
        merged_transcript_artifact=ArtifactRef(
            id=f"{SESSION_ID}:artifacts/merged_transcript/merged.json",
            artifact_type="merged_transcript",
            version=1,
            sha256="1" * 64,
        ),
        manual_notes_artifact=None,
        policy=SummarizationPolicy(language="en").to_dict(),
        provider=FakeSummarizationProvider(result=_summary_result()).metadata,
        timeline=_summary_result().timeline,
        action_items=_summary_result().action_items,
        general_summary=_summary_result().general_summary,
        created_at="2026-05-18T17:00:00Z",
        request_records=_summary_result().request_records,
    )
    payload = artifact.to_dict()
    cast(dict[str, object], payload["policy"])["prompt_policy"] = "meeting-summary-json-v1"
    cast(dict[str, object], payload["provider"])["prompt_policy"] = "meeting-summary-json-v1"

    loaded = SummaryArtifact.from_dict(payload)

    assert loaded.policy["prompt_policy"] == "meeting-summary-json-v1"
    assert loaded.provider.prompt_policy == "meeting-summary-json-v1"
    Draft202012Validator(_load_summary_schema(), format_checker=FormatChecker()).validate(payload)

    payload = artifact.to_dict()
    cast(dict[str, object], payload["policy"])["prompt_policy"] = SCHEMA_PROMPT_POLICY
    cast(dict[str, object], payload["provider"])["prompt_policy"] = SCHEMA_PROMPT_POLICY

    loaded = SummaryArtifact.from_dict(payload)

    assert loaded.policy["prompt_policy"] == SCHEMA_PROMPT_POLICY
    assert loaded.provider.prompt_policy == SCHEMA_PROMPT_POLICY


def test_summary_schema_validates_contract() -> None:
    artifact = SummaryArtifact(
        version=SUMMARY_SCHEMA_VERSION,
        session_id=SESSION_ID,
        merged_transcript_artifact=ArtifactRef(
            id=f"{SESSION_ID}:artifacts/merged_transcript/merged.json",
            artifact_type="merged_transcript",
            version=1,
            sha256="1" * 64,
        ),
        manual_notes_artifact=None,
        policy=SummarizationPolicy(language="en").to_dict(),
        provider=FakeSummarizationProvider(result=_summary_result()).metadata,
        timeline=_summary_result().timeline,
        action_items=_summary_result().action_items,
        general_summary=_summary_result().general_summary,
        created_at="2026-05-18T17:00:00Z",
        request_records=_summary_result().request_records,
    )
    payload = artifact.to_dict()

    Draft202012Validator.check_schema(_load_summary_schema())
    Draft202012Validator(_load_summary_schema(), format_checker=FormatChecker()).validate(payload)


@pytest.mark.parametrize(
    "records",
    [
        [],
        [
            "timeline",
            "timeline",
            "general_summary",
        ],
    ],
)
def test_summary_schema_rejects_incomplete_request_records(records: list[str]) -> None:
    artifact = SummaryArtifact(
        version=SUMMARY_SCHEMA_VERSION,
        session_id=SESSION_ID,
        merged_transcript_artifact=ArtifactRef(
            id=f"{SESSION_ID}:artifacts/merged_transcript/merged.json",
            artifact_type="merged_transcript",
            version=1,
            sha256="1" * 64,
        ),
        manual_notes_artifact=None,
        policy=SummarizationPolicy(language="en").to_dict(),
        provider=FakeSummarizationProvider(result=_summary_result()).metadata,
        timeline=_summary_result().timeline,
        action_items=_summary_result().action_items,
        general_summary=_summary_result().general_summary,
        created_at="2026-05-18T17:00:00Z",
        request_records=_request_records_for_products(tuple(records)),
    )
    payload = artifact.to_dict()

    validator = Draft202012Validator(_load_summary_schema(), format_checker=FormatChecker())
    with pytest.raises(ValidationError):
        validator.validate(payload)


@pytest.mark.parametrize(
    "case",
    ["duplicate_one_shot", "maps_without_reduce", "mixed_strategy"],
)
def test_summary_schema_rejects_incoherent_strategy_shapes(case: str) -> None:
    reducer_records = _reducer_records_for_product("timeline")
    records_by_case: dict[str, tuple[ProductRequestRecord, ...]] = {
        "duplicate_one_shot": (
            *_request_records_for_products(REQUIRED_NOTE_PRODUCTS),
            *_request_records_for_products(("timeline",)),
        ),
        "maps_without_reduce": (
            *(record for record in reducer_records if record.call == "map"),
            *_request_records_for_products(("action_items", "general_summary")),
        ),
        "mixed_strategy": (
            *reducer_records,
            *_request_records_for_products(REQUIRED_NOTE_PRODUCTS),
        ),
    }
    payload = _summary_artifact_with_records(records_by_case[case]).to_dict()

    validator = Draft202012Validator(_load_summary_schema(), format_checker=FormatChecker())
    with pytest.raises(ValidationError):
        validator.validate(payload)


@pytest.mark.parametrize(
    ("call", "overrides"),
    [
        ("one_shot", {"chunk_index": 0}),
        ("one_shot", {"strategy": "reducer"}),
        ("map", {"chunk_count": None}),
        ("reduce", {"chunk_index": 0}),
    ],
)
def test_summary_schema_rejects_incoherent_record_fields(
    call: str,
    overrides: dict[str, object],
) -> None:
    records = (
        *_reducer_records_for_product("timeline"),
        *_request_records_for_products(("action_items", "general_summary")),
    )
    payload = _summary_artifact_with_records(records).to_dict()
    raw_records = cast(list[dict[str, object]], payload["request_records"])
    target = next(item for item in raw_records if item["call"] == call)
    target.update(overrides)

    validator = Draft202012Validator(_load_summary_schema(), format_checker=FormatChecker())
    with pytest.raises(ValidationError):
        validator.validate(payload)


def test_summary_schema_rejects_legacy_provider_config_inside_policy() -> None:
    artifact = SummaryArtifact(
        version=SUMMARY_SCHEMA_VERSION,
        session_id=SESSION_ID,
        merged_transcript_artifact=ArtifactRef(
            id=f"{SESSION_ID}:artifacts/merged_transcript/merged.json",
            artifact_type="merged_transcript",
            version=1,
            sha256="1" * 64,
        ),
        manual_notes_artifact=None,
        policy=SummarizationPolicy(language="en").to_dict(),
        provider=FakeSummarizationProvider(result=_summary_result()).metadata,
        timeline=_summary_result().timeline,
        action_items=_summary_result().action_items,
        general_summary=_summary_result().general_summary,
        created_at="2026-05-18T17:00:00Z",
        request_records=_summary_result().request_records,
    )
    payload = artifact.to_dict()
    cast(dict[str, object], payload["policy"])["provider_config_id"] = "provider"

    validator = Draft202012Validator(_load_summary_schema(), format_checker=FormatChecker())
    with pytest.raises(ValidationError, match="Additional properties"):
        validator.validate(payload)


def test_summary_schema_accepts_mistral_provider_provenance() -> None:
    artifact = SummaryArtifact(
        version=SUMMARY_SCHEMA_VERSION,
        session_id=SESSION_ID,
        merged_transcript_artifact=ArtifactRef(
            id=f"{SESSION_ID}:artifacts/merged_transcript/merged.json",
            artifact_type="merged_transcript",
            version=1,
            sha256="1" * 64,
        ),
        manual_notes_artifact=None,
        policy=SummarizationPolicy(language="en").to_dict(),
        provider=SummarizationProviderMetadata(
            name="mistral",
            model="mistral-small-latest",
            endpoint="https://api.mistral.ai",
            prompt_policy=PROMPT_POLICY,
            resolved_model="mistral-small-2603",
            account_scope="mistral-account:abc123",
        ),
        timeline=_summary_result().timeline,
        action_items=_summary_result().action_items,
        general_summary=_summary_result().general_summary,
        created_at="2026-05-18T17:00:00Z",
        request_records=_summary_result().request_records,
    )

    Draft202012Validator(_load_summary_schema(), format_checker=FormatChecker()).validate(
        artifact.to_dict()
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://ollama.test",
        "http://user:secret@localhost:11434",
        "http://localhost:11434?token=secret",
        "http://localhost:11434/#fragment",
        "http://localhost:notaport",
        "http://[::1",
    ],
)
def test_ollama_provider_rejects_remote_secret_or_malformed_endpoints(endpoint: str) -> None:
    with pytest.raises(SummarizationContractError):
        OllamaSummarizationProvider(endpoint=endpoint)


def test_ollama_summarization_provider_marks_resource_used_before_failed_chat_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSummarizationProvider(
        model="test-model",
        endpoint="http://localhost:11434",
    )

    def fake_urlopen(_request: object, *, timeout: float) -> object:
        _ = timeout
        raise OSError("connection refused")

    monkeypatch.setattr("mena.local_llm.urllib.request.urlopen", fake_urlopen)

    with pytest.raises(SummarizationProviderUnavailable, match="provider is unavailable"):
        provider._post_json("/api/chat", {"model": "test-model"})

    assert provider.runtime_resource_used is True


def test_ollama_summarization_provider_releases_runtime_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSummarizationProvider(
        model="test-model",
        endpoint="http://localhost:11434",
    )
    captured: dict[str, object] = {}

    def fake_unload_ollama_model(
        *,
        endpoint: str,
        model: str,
        timeout_seconds: float,
    ) -> str:
        captured["endpoint"] = endpoint
        captured["model"] = model
        captured["timeout_seconds"] = timeout_seconds
        return "unload"

    monkeypatch.setattr(
        "mena.ollama_summarization.unload_ollama_model",
        fake_unload_ollama_model,
    )
    lease = RuntimeLease(
        resource_kind="local_llm",
        stage="summarization",
        boundary="summarize",
        provider="ollama",
        model="test-model",
    )

    result = provider.release_runtime_resource(lease)

    assert result.status == "released"
    assert result.lease == lease
    assert result.detail == "Ollama model release completed: unload."
    assert captured == {
        "endpoint": "http://localhost:11434",
        "model": "test-model",
        "timeout_seconds": provider.timeout_seconds,
    }


def _persist_merged_transcript(
    *,
    store: FileSystemArtifactStore | InMemoryArtifactStore,
) -> ArtifactRecord:
    return persist_merged_transcript_artifact(
        store=store,
        artifact=_merged_transcript_fixture(),
        name="merged.json",
    )


def _persist_notes(*, store: FileSystemArtifactStore | InMemoryArtifactStore) -> ArtifactRecord:
    return store.store(
        artifact_type="manual_notes",
        name="notes.md",
        data=b"- Manual note: release is blocked.",
        version=1,
        metadata={"contract": "manual-notes.raw"},
    )


def _persist_correction_overlay(
    *,
    store: FileSystemArtifactStore | InMemoryArtifactStore,
    source_record: ArtifactRecord,
    entries: tuple[CorrectionEntry, ...],
    name: str,
) -> ArtifactRecord:
    overlay = create_correction_overlay(
        session_id=SESSION_ID,
        source_transcript_artifact=ArtifactRef.from_record(source_record),
        entries=entries,
        now=NOW,
    )
    return persist_correction_overlay_artifact(
        store=store,
        artifact=overlay,
        name=name,
        metadata={"workflow_status": "completed", "completed_at": "2026-05-18T17:00:00Z"},
    )


def _summary_request() -> SummarizationRequest:
    return SummarizationRequest(
        session_id=SESSION_ID,
        merged_transcript=_merged_transcript_fixture(),
        manual_notes="- Manual note",
        policy=SummarizationPolicy(language="en"),
    )


def _summary_result() -> SummaryResult:
    return SummaryResult(
        timeline=(
            SummaryTimelineEntry(
                text="Discussed database migration before Friday.",
                source_segment_ids=("merged_segment_000001",),
                start_seconds=0.0,
            ),
        ),
        action_items=(
            SummaryActionItem(text="Finish database migration.", owner="You", due="Friday"),
        ),
        general_summary="The meeting focused on database migration risk.",
        request_records=_request_records_for_products(REQUIRED_NOTE_PRODUCTS),
    )


def _request_records_for_products(products: tuple[str, ...]) -> tuple[ProductRequestRecord, ...]:
    return tuple(
        ProductRequestRecord(
            product=product,
            model="fake-summarizer",
            num_ctx=OLLAMA_SUMMARIZATION_NUM_CTX,
            temperature=0,
            seed=OLLAMA_SUMMARIZATION_SEED,
            prompt_eval_count=None,
            eval_count=None,
        )
        for product in products
    )


def _merged_transcript_fixture() -> MergedTranscriptArtifact:
    transcript_ref = ArtifactRef(
        id=f"{SESSION_ID}:artifacts/transcript/merged-source.json",
        artifact_type="transcript",
        version=1,
        sha256="1" * 64,
    )
    return MergedTranscriptArtifact(
        version=MERGED_TRANSCRIPT_SCHEMA_VERSION,
        session_id=SESSION_ID,
        policy=MergePolicy(),
        sources=(
            MergedTranscriptSource(
                source_id="local_mic",
                channel="local",
                transcript_artifact=transcript_ref,
                diarization_status="not_applicable",
                diarization_reason="local_user_structural_attribution",
            ),
            MergedTranscriptSource(
                source_id="system_audio",
                channel="remote",
                transcript_artifact=transcript_ref,
                diarization_status="skipped",
                diarization_reason="diarization_disabled",
            ),
        ),
        text=(
            "We need to migrate the customer database before Friday. "
            "Otherwise the release is blocked."
        ),
        segments=(
            MergedTranscriptSegment(
                id="merged_segment_000001",
                text="We need to migrate the customer database before Friday.",
                start_seconds=0.0,
                end_seconds=2.0,
                source_id="local_mic",
                channel="local",
                speaker=_speaker("you", "You", "local_user"),
                transcription_confidence=_word_confidence(),
                overlap_detected=False,
                transcript_segment_id="local_segment_1",
                metadata={},
            ),
            MergedTranscriptSegment(
                id="merged_segment_000002",
                text="Otherwise the release is blocked.",
                start_seconds=2.1,
                end_seconds=4.0,
                source_id="system_audio",
                channel="remote",
                speaker=_speaker("remote", "Remote", "fallback_remote"),
                transcription_confidence=_word_confidence(),
                overlap_detected=False,
                transcript_segment_id="remote_segment_1",
                metadata={},
            ),
        ),
        created_at="2026-05-18T17:00:00Z",
    )


def _speaker(speaker_id: str, label: str, kind: str) -> SpeakerIdentity:
    return SpeakerIdentity(
        speaker_id=speaker_id,
        label=label,
        kind=kind,
        confidence=SegmentConfidence(
            status="available" if kind == "local_user" else "unavailable",
            metric="structural" if kind == "local_user" else None,
            value=1.0 if kind == "local_user" else None,
            reason="fixture",
        ),
    )


def _word_confidence() -> SegmentConfidence:
    return SegmentConfidence(
        status="available",
        metric="word_probability",
        value=0.98,
        reason="fixture",
    )


def _load_summary_schema() -> dict[str, object]:
    schema_path = files("mena.schemas").joinpath("summary.v3.schema.json")
    return cast(dict[str, object], json.loads(schema_path.read_text(encoding="utf-8")))
