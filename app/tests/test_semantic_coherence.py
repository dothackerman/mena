import json
from dataclasses import replace
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator, FormatChecker

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
from mena.semantic_coherence import (
    SEMANTIC_COHERENCE_STAGE,
    FakeSemanticCoherenceProvider,
    OllamaSemanticCoherenceProvider,
    SemanticCoherenceContractError,
    SemanticCoherenceError,
    SemanticCoherencePolicy,
    SemanticCoherenceProviderUnavailable,
    SemanticCoherenceRequest,
    SemanticCoherenceResult,
    run_semantic_coherence_stage,
)

SESSION_ID = "00000000-0000-0000-0000-000000000001"
RUN_ID = "run-20260518-semantic"
NOW = datetime(2026, 5, 18, 10, 0, 0, tzinfo=UTC)


def test_semantic_coherence_stage_persists_review_flags_and_checkpoint(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    merged_record = _persist_merged_transcript(store=store)
    provider = FakeSemanticCoherenceProvider(
        results={
            "merged_segment_000002": SemanticCoherenceResult(
                coherent=False,
                confidence=0.82,
                reason="topic-breaking phrase in surrounding migration discussion",
                evidence={"phrase": "tomato bracelet"},
            )
        }
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    result = run_semantic_coherence_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        provider=provider,
        now=NOW,
    )

    assert result.skipped is False
    assert result.state.status == "completed"
    payload = json.loads(store.retrieve(result.review_queue_record.id))
    Draft202012Validator(
        _load_review_queue_schema(),
        format_checker=FormatChecker(),
    ).validate(payload)
    assert result.review_queue_record.artifact_type == "review_queue"
    assert result.review_queue_record.version == 1
    policy = cast(dict[str, object], result.review_queue.policy)
    assert policy["provider_config_id"] == provider.metadata.config_id
    assert len(result.review_queue.flags) == 1
    flag = result.review_queue.flags[0]
    assert flag.reason_code == "semantic_incoherence"
    assert flag.segment_id == "merged_segment_000002"
    assert flag.metadata["confidence"] == pytest.approx(0.82)
    assert cast(dict[str, object], flag.metadata["provider"])["name"] == "fake"
    assert [state.status for state in ledger.states(stage=SEMANTIC_COHERENCE_STAGE)] == [
        "running",
        "completed",
    ]


def test_semantic_coherence_stage_reuses_verified_completed_output() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(store=store)
    provider = FakeSemanticCoherenceProvider(
        results={
            "merged_segment_000002": SemanticCoherenceResult(
                coherent=False,
                confidence=0.9,
                reason="fixture incoherent",
                evidence={},
            )
        }
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    first = run_semantic_coherence_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        provider=provider,
        now=NOW,
    )
    second = run_semantic_coherence_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        provider=provider,
        now=NOW,
    )

    assert second.skipped is True
    assert second.skip_reason == "completed_output_reused"
    assert second.review_queue_record == first.review_queue_record


def test_semantic_coherence_stage_respects_policy_threshold_and_provider_config() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(store=store)
    provider = FakeSemanticCoherenceProvider(
        results={
            "merged_segment_000002": SemanticCoherenceResult(
                coherent=False,
                confidence=0.7,
                reason="fixture incoherent",
                evidence={},
            )
        },
        model="fake-a",
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    first = run_semantic_coherence_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        provider=provider,
        policy=SemanticCoherencePolicy(min_flag_confidence=0.8),
        now=NOW,
    )
    second = run_semantic_coherence_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        provider=FakeSemanticCoherenceProvider(results={}, model="fake-b"),
        policy=SemanticCoherencePolicy(min_flag_confidence=0.8),
        attempt=2,
        now=NOW,
    )

    assert first.review_queue.flags == ()
    assert second.skipped is False


def test_semantic_coherence_stage_marks_failed_checkpoint_when_provider_unavailable() -> None:
    class UnavailableProvider:
        metadata = FakeSemanticCoherenceProvider(results={}).metadata

        def check(self, request: object) -> SemanticCoherenceResult:
            raise SemanticCoherenceProviderUnavailable("provider unavailable")

    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = _persist_merged_transcript(store=store)
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    with pytest.raises(SemanticCoherenceError, match="provider unavailable"):
        run_semantic_coherence_stage(
            store=store,
            ledger=ledger,
            merged_transcript_record=merged_record,
            provider=UnavailableProvider(),
            now=NOW,
        )

    failed = ledger.states(stage=SEMANTIC_COHERENCE_STAGE)[-1]
    assert failed.status == "failed"
    assert failed.error is not None
    assert failed.error.retryable is True
    assert failed.error.code == "semantic_coherence_failed"


def test_semantic_coherence_stage_fails_on_session_mismatch() -> None:
    store = InMemoryArtifactStore(session_id=SESSION_ID)
    merged_record = persist_merged_transcript_artifact(
        store=store,
        artifact=replace(
            _merged_transcript_fixture(),
            session_id="00000000-0000-0000-0000-000000000099",
        ),
        name="merged.json",
    )
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    with pytest.raises(SemanticCoherenceError, match="session_id must match"):
        run_semantic_coherence_stage(
            store=store,
            ledger=ledger,
            merged_transcript_record=merged_record,
            provider=FakeSemanticCoherenceProvider(results={}),
            now=NOW,
        )

    failed = ledger.states(stage=SEMANTIC_COHERENCE_STAGE)[-1]
    assert failed.status == "failed"
    assert failed.error is not None
    assert failed.error.retryable is False


def test_semantic_coherence_stage_ignores_corrupt_reusable_output(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    merged_record = _persist_merged_transcript(store=store)
    provider = FakeSemanticCoherenceProvider(results={})
    ledger = ProcessingStageLedger(store=store, session_id=SESSION_ID, run_id=RUN_ID)

    first = run_semantic_coherence_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        provider=provider,
        now=NOW,
    )
    (tmp_path / first.review_queue_record.path).write_bytes(b"corrupt")
    second = run_semantic_coherence_stage(
        store=store,
        ledger=ledger,
        merged_transcript_record=merged_record,
        provider=provider,
        attempt=2,
        now=NOW,
    )

    assert second.skipped is False
    assert second.review_queue_record.id != first.review_queue_record.id


def test_ollama_semantic_provider_parses_strict_json_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSemanticCoherenceProvider(model="test-model")

    def fake_post_json(path: str, payload: object) -> dict[str, object]:
        assert path == "/api/chat"
        assert isinstance(payload, dict)
        assert payload["model"] == "test-model"
        return {
            "message": {
                "content": json.dumps(
                    {
                        "coherent": False,
                        "confidence": 0.77,
                        "reason": "context mismatch",
                        "evidence": {"window": "previous/current/next"},
                    }
                )
            }
        }

    monkeypatch.setattr(provider, "_post_json", fake_post_json)

    result = provider.check(_semantic_request())

    assert result.coherent is False
    assert result.confidence == pytest.approx(0.77)
    assert result.reason == "context mismatch"


def test_ollama_semantic_provider_accepts_fenced_json_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSemanticCoherenceProvider(model="test-model")

    def fake_post_json(path: str, payload: object) -> dict[str, object]:
        return {
            "message": {
                "content": """```json
{"coherent": true, "confidence": 0.91, "reason": "fits context", "evidence": null}
```"""
            }
        }

    monkeypatch.setattr(provider, "_post_json", fake_post_json)

    result = provider.check(_semantic_request())

    assert result.coherent is True
    assert result.evidence == {}


def test_ollama_semantic_provider_rejects_invalid_json_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSemanticCoherenceProvider(model="test-model")

    def fake_post_json(path: str, payload: object) -> dict[str, object]:
        return {"message": {"content": "not-json"}}

    monkeypatch.setattr(provider, "_post_json", fake_post_json)

    with pytest.raises(SemanticCoherenceContractError, match="not valid JSON"):
        provider.check(_semantic_request())


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({}, "missing message object"),
        ({"message": {}}, "message content is empty"),
        ({"message": {"content": ""}}, "message content is empty"),
    ],
)
def test_ollama_semantic_provider_rejects_invalid_envelope_content(
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, object],
    message: str,
) -> None:
    provider = OllamaSemanticCoherenceProvider(model="test-model")

    def fake_post_json(path: str, payload: object) -> dict[str, object]:
        return response

    monkeypatch.setattr(provider, "_post_json", fake_post_json)

    with pytest.raises(SemanticCoherenceContractError, match=message):
        provider.check(_semantic_request())


def test_ollama_post_json_handles_success_invalid_json_and_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OllamaSemanticCoherenceProvider(model="test-model", endpoint="http://ollama.test")

    class FakeResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return self._body

    def good_urlopen(request: object, *, timeout: float) -> FakeResponse:
        assert timeout == provider.timeout_seconds
        return FakeResponse(b'{"ok": true}')

    monkeypatch.setattr("mena.semantic_coherence.urllib.request.urlopen", good_urlopen)
    assert provider._post_json("/api/chat", {"model": "test-model"}) == {"ok": True}

    def bad_json_urlopen(request: object, *, timeout: float) -> FakeResponse:
        return FakeResponse(b"not-json")

    monkeypatch.setattr("mena.semantic_coherence.urllib.request.urlopen", bad_json_urlopen)
    with pytest.raises(SemanticCoherenceContractError, match="envelope was not JSON"):
        provider._post_json("/api/chat", {"model": "test-model"})

    def unavailable_urlopen(request: object, *, timeout: float) -> FakeResponse:
        raise OSError("connection refused")

    monkeypatch.setattr("mena.semantic_coherence.urllib.request.urlopen", unavailable_urlopen)
    with pytest.raises(SemanticCoherenceProviderUnavailable, match="provider is unavailable"):
        provider._post_json("/api/chat", {"model": "test-model"})


def test_semantic_coherence_policy_from_dict_validates_contract() -> None:
    policy = SemanticCoherencePolicy.from_dict(
        {
            "policy_version": 1,
            "context_window_segments": 2,
            "min_flag_confidence": 0.7,
            "prompt_policy": "semantic-coherence-json-v1",
        }
    )

    assert policy.context_window_segments == 2
    assert policy.to_dict(provider_config_id="provider")["provider_config_id"] == "provider"

    with pytest.raises(SemanticCoherenceContractError, match="Unsupported semantic coherence"):
        SemanticCoherencePolicy.from_dict(
            {
                "policy_version": 99,
                "context_window_segments": 1,
                "min_flag_confidence": 0.7,
                "prompt_policy": "semantic-coherence-json-v1",
            }
        )
    with pytest.raises(SemanticCoherenceContractError, match="must be non-negative"):
        SemanticCoherencePolicy.from_dict(
            {
                "policy_version": 1,
                "context_window_segments": -1,
                "min_flag_confidence": 0.7,
                "prompt_policy": "semantic-coherence-json-v1",
            }
        )
    with pytest.raises(SemanticCoherenceContractError, match="must be within"):
        SemanticCoherencePolicy.from_dict(
            {
                "policy_version": 1,
                "context_window_segments": 1,
                "min_flag_confidence": 1.7,
                "prompt_policy": "semantic-coherence-json-v1",
            }
        )


def test_semantic_coherence_result_from_dict_validates_contract() -> None:
    result = SemanticCoherenceResult.from_dict(
        {
            "coherent": False,
            "confidence": 0.66,
            "reason": "context mismatch",
        }
    )

    assert result.evidence == {}

    with pytest.raises(SemanticCoherenceContractError, match="must be a boolean"):
        SemanticCoherenceResult.from_dict(
            {
                "coherent": "false",
                "confidence": 0.66,
                "reason": "context mismatch",
            }
        )
    with pytest.raises(SemanticCoherenceContractError, match="must be an object"):
        SemanticCoherenceResult.from_dict(
            {
                "coherent": False,
                "confidence": 0.66,
                "reason": "context mismatch",
                "evidence": "bad",
            }
        )


def _persist_merged_transcript(
    *,
    store: FileSystemArtifactStore | InMemoryArtifactStore,
) -> ArtifactRecord:
    return persist_merged_transcript_artifact(
        store=store,
        artifact=_merged_transcript_fixture(),
        name="merged.json",
    )


def _semantic_request() -> SemanticCoherenceRequest:
    artifact = _merged_transcript_fixture()

    return SemanticCoherenceRequest(
        session_id=SESSION_ID,
        segment=artifact.segments[1],
        previous_segments=(artifact.segments[0],),
        next_segments=(artifact.segments[2],),
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
            "The tomato bracelet should be migrated before Friday. "
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
                text="The tomato bracelet should be migrated before Friday.",
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
            MergedTranscriptSegment(
                id="merged_segment_000003",
                text="Otherwise the release is blocked.",
                start_seconds=4.1,
                end_seconds=5.2,
                source_id="system_audio",
                channel="remote",
                speaker=_speaker("remote", "Remote", "fallback_remote"),
                transcription_confidence=_word_confidence(),
                overlap_detected=False,
                transcript_segment_id="remote_segment_2",
                metadata={},
            ),
        ),
        created_at="2026-05-18T10:00:00Z",
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


def _load_review_queue_schema() -> dict[str, object]:
    schema_path = files("mena.schemas").joinpath("review-queue.v1.schema.json")
    return cast(dict[str, object], json.loads(schema_path.read_text(encoding="utf-8")))
