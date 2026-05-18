import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from mena.artifacts import ArtifactRecord, FileSystemArtifactStore, InMemoryArtifactStore
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
from mena.processing import ArtifactRef, ProcessingStageLedger
from mena.summarization import (
    SUMMARIZATION_STAGE,
    FakeSummarizationProvider,
    OllamaSummarizationProvider,
    SummarizationContractError,
    SummarizationPolicy,
    SummarizationProviderUnavailable,
    SummarizationRequest,
    SummaryActionItem,
    SummaryArtifact,
    SummaryResult,
    run_summarization_stage,
)

SESSION_ID = "00000000-0000-0000-0000-000000000001"
RUN_ID = "run-20260518-summary"
NOW = datetime(2026, 5, 18, 17, 0, 0, tzinfo=UTC)


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
    assert result.summary_record.metadata["contract"] == "summary.v1"
    payload = json.loads(store.retrieve(result.summary_record.id))
    artifact = SummaryArtifact.from_dict(payload)
    assert artifact.provider.name == "fake"
    assert artifact.timeline == ("Discussed database migration before Friday.",)
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


def test_ollama_summarization_provider_parses_strict_json_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSummarizationProvider(model="test-model")

    def fake_post_json(path: str, payload: object) -> dict[str, object]:
        assert path == "/api/chat"
        assert isinstance(payload, dict)
        assert payload["model"] == "test-model"
        messages = cast(list[dict[str, str]], payload["messages"])
        assert "meeting_summary" in messages[1]["content"]
        return {"message": {"content": json.dumps(_summary_result().to_dict())}}

    monkeypatch.setattr(provider, "_post_json", fake_post_json)

    result = provider.summarize(_summary_request())

    assert result.general_summary == "The meeting focused on database migration risk."


def test_ollama_summarization_provider_rejects_invalid_json_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSummarizationProvider(model="test-model")

    def fake_post_json(path: str, payload: object) -> dict[str, object]:
        return {"message": {"content": "not-json"}}

    monkeypatch.setattr(provider, "_post_json", fake_post_json)

    with pytest.raises(SummarizationContractError, match="not valid JSON"):
        provider.summarize(_summary_request())


def test_summarization_policy_and_result_validate_contract() -> None:
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
                "important_points": ["Important"],
                "action_items": [],
                "general_summary": "Summary",
            }
        )


def test_summary_artifact_from_dict_validates_contract() -> None:
    artifact = SummaryArtifact(
        version=1,
        session_id=SESSION_ID,
        merged_transcript_artifact=ArtifactRef(
            id=f"{SESSION_ID}:artifacts/merged_transcript/merged.json",
            artifact_type="merged_transcript",
            version=1,
            sha256="1" * 64,
        ),
        manual_notes_artifact=None,
        policy=SummarizationPolicy(language="en").to_dict(provider_config_id="provider"),
        provider=FakeSummarizationProvider(result=_summary_result()).metadata,
        timeline=_summary_result().timeline,
        important_points=_summary_result().important_points,
        action_items=_summary_result().action_items,
        general_summary=_summary_result().general_summary,
        created_at="2026-05-18T17:00:00Z",
    )
    payload = artifact.to_dict()
    payload["version"] = 99

    with pytest.raises(SummarizationContractError, match="Unsupported summary"):
        SummaryArtifact.from_dict(payload)

    payload = artifact.to_dict()
    cast(dict[str, object], payload["provider"])["prompt_policy"] = "other"
    with pytest.raises(SummarizationContractError, match="Unsupported summary provider"):
        SummaryArtifact.from_dict(payload)


def test_ollama_provider_rejects_remote_or_secret_bearing_endpoints() -> None:
    with pytest.raises(SummarizationContractError, match="must be local"):
        OllamaSummarizationProvider(endpoint="http://ollama.test")


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


def _summary_request() -> SummarizationRequest:
    return SummarizationRequest(
        session_id=SESSION_ID,
        merged_transcript=_merged_transcript_fixture(),
        manual_notes="- Manual note",
        policy=SummarizationPolicy(language="en"),
    )


def _summary_result() -> SummaryResult:
    return SummaryResult(
        timeline=("Discussed database migration before Friday.",),
        important_points=("Release is blocked until migration is complete.",),
        action_items=(
            SummaryActionItem(text="Finish database migration.", owner="You", due="Friday"),
        ),
        general_summary="The meeting focused on database migration risk.",
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
