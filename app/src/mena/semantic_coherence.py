"""Semantic coherence review flagging and local Ollama adapter."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol, Self

from mena.artifacts import ArtifactRecord, ArtifactStore
from mena.merge_timing import (
    MergedTranscriptArtifact,
    MergedTranscriptSegment,
    MergeTimingContractError,
)
from mena.processing import (
    ArtifactRef,
    ProcessingErrorInfo,
    ProcessingStageLedger,
    ProcessingStageState,
)
from mena.review_flagging import (
    REVIEW_QUEUE_SCHEMA_VERSION,
    ReviewFlag,
    ReviewFlaggingError,
    ReviewQueueArtifact,
    persist_review_queue_artifact,
)

SEMANTIC_COHERENCE_STAGE = "semantic_coherence"
SEMANTIC_COHERENCE_POLICY_VERSION = 1
SEMANTIC_INCOHERENCE_REASON = "semantic_incoherence"
OLLAMA_DEFAULT_ENDPOINT = "http://localhost:11434"
OLLAMA_DEFAULT_MODEL = "llama3.2:3b"
PROMPT_POLICY = "semantic-coherence-json-v1"


class SemanticCoherenceError(RuntimeError):
    """Base class for semantic coherence failures."""


class SemanticCoherenceContractError(SemanticCoherenceError):
    """Raised when semantic coherence payloads violate the v1 contract."""


class SemanticCoherenceProviderUnavailable(SemanticCoherenceError):
    """Raised when a semantic coherence provider cannot be reached."""


@dataclass(frozen=True)
class SemanticCoherencePolicy:
    """Deterministic policy for semantic review flag generation."""

    policy_version: int = SEMANTIC_COHERENCE_POLICY_VERSION
    context_window_segments: int = 1
    min_flag_confidence: float = 0.65
    prompt_policy: str = PROMPT_POLICY

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Semantic coherence policy")
        policy = cls(
            policy_version=_required_int(item, "policy_version", "Semantic coherence policy"),
            context_window_segments=_required_int(
                item,
                "context_window_segments",
                "Semantic coherence policy",
            ),
            min_flag_confidence=_required_number(
                item,
                "min_flag_confidence",
                "Semantic coherence policy",
            ),
            prompt_policy=_required_str(item, "prompt_policy", "Semantic coherence policy"),
        )
        if policy.policy_version != SEMANTIC_COHERENCE_POLICY_VERSION:
            raise SemanticCoherenceContractError("Unsupported semantic coherence policy version.")
        if policy.context_window_segments < 0:
            raise SemanticCoherenceContractError(
                "Semantic coherence context_window_segments must be non-negative."
            )
        if policy.prompt_policy != PROMPT_POLICY:
            raise SemanticCoherenceContractError("Unsupported semantic coherence prompt policy.")
        _validate_probability(policy.min_flag_confidence, "min_flag_confidence")
        return policy

    def config_id(self, *, provider_config_id: str) -> str:
        return json.dumps(
            self.to_dict(provider_config_id=provider_config_id),
            sort_keys=True,
            separators=(",", ":"),
        )

    def to_dict(self, *, provider_config_id: str) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "context_window_segments": self.context_window_segments,
            "min_flag_confidence": self.min_flag_confidence,
            "prompt_policy": self.prompt_policy,
            "provider_config_id": provider_config_id,
        }


@dataclass(frozen=True)
class SemanticCoherenceProviderMetadata:
    """Provider identity persisted with semantic review flags."""

    name: str
    model: str
    endpoint: str | None = None
    prompt_policy: str = PROMPT_POLICY

    @property
    def config_id(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "model": self.model,
            "endpoint": self.endpoint,
            "prompt_policy": self.prompt_policy,
        }


@dataclass(frozen=True)
class SemanticCoherenceRequest:
    """One segment plus transcript context sent to a semantic provider."""

    session_id: str
    segment: MergedTranscriptSegment
    previous_segments: tuple[MergedTranscriptSegment, ...]
    next_segments: tuple[MergedTranscriptSegment, ...]


@dataclass(frozen=True)
class SemanticCoherenceResult:
    """Provider result for one segment."""

    coherent: bool
    confidence: float
    reason: str
    evidence: Mapping[str, object]

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Semantic coherence result")
        result = cls(
            coherent=_required_bool(item, "coherent", "Semantic coherence result"),
            confidence=_required_number(item, "confidence", "Semantic coherence result"),
            reason=_required_str(item, "reason", "Semantic coherence result"),
            evidence=_optional_json_object(item, "evidence", "Semantic coherence result"),
        )
        _validate_probability(result.confidence, "Semantic coherence confidence")
        return result


class SemanticCoherenceProvider(Protocol):
    """Provider port for transcript-context coherence checks."""

    metadata: SemanticCoherenceProviderMetadata

    def check(self, request: SemanticCoherenceRequest) -> SemanticCoherenceResult:
        """Return one semantic coherence judgment for a segment."""


class FakeSemanticCoherenceProvider:
    """Deterministic provider for tests and local contract fixtures."""

    def __init__(
        self,
        *,
        results: Mapping[str, SemanticCoherenceResult],
        model: str = "fake-semantic-coherence",
    ) -> None:
        self.metadata = SemanticCoherenceProviderMetadata(name="fake", model=model)
        self._results = dict(results)

    def check(self, request: SemanticCoherenceRequest) -> SemanticCoherenceResult:
        return self._results.get(
            request.segment.id,
            SemanticCoherenceResult(
                coherent=True,
                confidence=1.0,
                reason="fixture_default_coherent",
                evidence={"source": "fake_default"},
            ),
        )


class OllamaSemanticCoherenceProvider:
    """Local Ollama adapter for semantic coherence checks."""

    def __init__(
        self,
        *,
        model: str = OLLAMA_DEFAULT_MODEL,
        endpoint: str = OLLAMA_DEFAULT_ENDPOINT,
        timeout_seconds: float = 30.0,
    ) -> None:
        sanitized_endpoint = _local_ollama_endpoint(endpoint)
        self.metadata = SemanticCoherenceProviderMetadata(
            name="ollama",
            model=model,
            endpoint=sanitized_endpoint,
        )
        self.timeout_seconds = timeout_seconds

    def check(self, request: SemanticCoherenceRequest) -> SemanticCoherenceResult:
        payload = {
            "model": self.metadata.model,
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are checking whether one transcript segment is semantically "
                        "coherent in its surrounding transcript context. Return strict JSON "
                        "only with keys coherent (boolean), confidence (0..1), reason "
                        "(short string), and evidence (object). Do not correct the transcript."
                    ),
                },
                {"role": "user", "content": _prompt(request)},
            ],
        }
        response = self._post_json("/api/chat", payload)
        content = _ollama_message_content(response)
        try:
            decoded = json.loads(_strip_json_fence(content))
        except json.JSONDecodeError as exc:
            raise SemanticCoherenceContractError(
                "Ollama semantic coherence response was not valid JSON."
            ) from exc
        return SemanticCoherenceResult.from_dict(decoded)

    def _post_json(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        url = f"{self.metadata.endpoint}{path}"
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except OSError as exc:
            raise SemanticCoherenceProviderUnavailable(
                "Ollama semantic coherence provider is unavailable."
            ) from exc
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SemanticCoherenceContractError("Ollama response envelope was not JSON.") from exc
        return _mapping(decoded, "Ollama response envelope")


@dataclass(frozen=True)
class SemanticCoherenceStageResult:
    """Result of running or reusing the semantic coherence stage."""

    state: ProcessingStageState
    review_queue: ReviewQueueArtifact
    review_queue_record: ArtifactRecord
    skipped: bool = False
    skip_reason: str | None = None


def run_semantic_coherence_stage(
    *,
    store: ArtifactStore,
    ledger: ProcessingStageLedger,
    merged_transcript_record: ArtifactRecord,
    provider: SemanticCoherenceProvider,
    policy: SemanticCoherencePolicy | None = None,
    stage: str = SEMANTIC_COHERENCE_STAGE,
    attempt: int = 1,
    skip_completed: bool = True,
    now: datetime | None = None,
) -> SemanticCoherenceStageResult:
    """Generate semantic incoherence review flags from a merged transcript artifact."""

    input_ref = ArtifactRef.from_record(merged_transcript_record)
    effective_policy = policy or SemanticCoherencePolicy()
    policy_payload = effective_policy.to_dict(provider_config_id=provider.metadata.config_id)
    policy_config_id = effective_policy.config_id(provider_config_id=provider.metadata.config_id)

    if skip_completed:
        completed = _completed_state_for_inputs(
            store=store,
            ledger=ledger,
            stage=stage,
            input_artifacts=(input_ref,),
            policy_config_id=policy_config_id,
        )
        if completed is not None:
            reused = _load_reusable_output(store=store, completed=completed)
            if reused is not None:
                record, artifact = reused
                timestamp = _timestamp(now)
                state = ledger.record(
                    ProcessingStageState(
                        version=1,
                        session_id=ledger.session_id,
                        run_id=ledger.run_id,
                        stage=stage,
                        status="skipped",
                        input_artifacts=(input_ref,),
                        output_artifacts=completed.output_artifacts,
                        started_at=timestamp,
                        completed_at=timestamp,
                        error=None,
                        attempt=attempt,
                    )
                )
                return SemanticCoherenceStageResult(
                    state=state,
                    review_queue=artifact,
                    review_queue_record=record,
                    skipped=True,
                    skip_reason="completed_output_reused",
                )

    started = ledger.mark_running(
        stage=stage,
        input_artifacts=(input_ref,),
        attempt=attempt,
        now=now,
    )
    try:
        merged_transcript = _verify_and_load_merged_transcript(
            store=store, record=merged_transcript_record
        )
        if merged_transcript.session_id != ledger.session_id:
            raise SemanticCoherenceContractError(
                "Merged transcript session_id must match the processing ledger."
            )
        review_queue = ReviewQueueArtifact(
            version=REVIEW_QUEUE_SCHEMA_VERSION,
            session_id=merged_transcript.session_id,
            merged_transcript_artifact=input_ref,
            policy=policy_payload,
            flags=_build_semantic_flags(
                merged_transcript=merged_transcript,
                provider=provider,
                policy=effective_policy,
            ),
            created_at=_timestamp(now),
        )
        review_record = persist_review_queue_artifact(
            store=store,
            artifact=review_queue,
            name=_review_queue_artifact_name(
                run_id=ledger.run_id,
                merged_transcript_sha256=merged_transcript_record.sha256,
                policy_config_id=policy_config_id,
                attempt=attempt,
            ),
        )
    except (
        SemanticCoherenceError,
        ReviewFlaggingError,
        MergeTimingContractError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        failed = _mark_failed(
            ledger=ledger,
            stage=stage,
            input_ref=input_ref,
            attempt=attempt,
            started_at=started.started_at,
            now=now,
            message=str(exc),
            retryable=isinstance(exc, SemanticCoherenceProviderUnavailable),
            code="semantic_coherence_failed",
        )
        message = failed.error.message if failed.error else str(exc)
        if isinstance(exc, SemanticCoherenceProviderUnavailable):
            raise SemanticCoherenceProviderUnavailable(message) from exc
        raise SemanticCoherenceError(message) from exc

    completed = ledger.mark_completed(
        stage=stage,
        input_artifacts=(input_ref,),
        output_artifacts=(ArtifactRef.from_record(review_record),),
        attempt=attempt,
        started_at=started.started_at,
        now=now,
    )
    return SemanticCoherenceStageResult(
        state=completed,
        review_queue=review_queue,
        review_queue_record=review_record,
    )


def _build_semantic_flags(
    *,
    merged_transcript: MergedTranscriptArtifact,
    provider: SemanticCoherenceProvider,
    policy: SemanticCoherencePolicy,
) -> tuple[ReviewFlag, ...]:
    flags: list[ReviewFlag] = []
    segments = merged_transcript.segments
    for index, segment in enumerate(segments):
        request = SemanticCoherenceRequest(
            session_id=merged_transcript.session_id,
            segment=segment,
            previous_segments=segments[max(0, index - policy.context_window_segments) : index],
            next_segments=segments[index + 1 : index + 1 + policy.context_window_segments],
        )
        result = provider.check(request)
        if result.coherent or result.confidence < policy.min_flag_confidence:
            continue
        flags.append(
            ReviewFlag(
                id=f"{segment.id}:{SEMANTIC_INCOHERENCE_REASON}",
                segment_id=segment.id,
                reason_code=SEMANTIC_INCOHERENCE_REASON,
                start_seconds=segment.start_seconds,
                end_seconds=segment.end_seconds,
                source_id=segment.source_id,
                speaker_id=segment.speaker.speaker_id,
                metadata={
                    "provider": provider.metadata.to_dict(),
                    "confidence": result.confidence,
                    "reason": result.reason,
                    "evidence": dict(result.evidence),
                    "context_window_segments": policy.context_window_segments,
                    "prompt_policy": policy.prompt_policy,
                },
            )
        )
    return tuple(flags)


def _verify_and_load_merged_transcript(
    *, store: ArtifactStore, record: ArtifactRecord
) -> MergedTranscriptArtifact:
    verification = store.verify(record.id)
    if not verification.ok:
        raise SemanticCoherenceContractError(
            verification.error or "merged transcript artifact failed verification"
        )
    return MergedTranscriptArtifact.from_dict(json.loads(store.retrieve(record.id)))


def _completed_state_for_inputs(
    *,
    store: ArtifactStore,
    ledger: ProcessingStageLedger,
    stage: str,
    input_artifacts: tuple[ArtifactRef, ...],
    policy_config_id: str,
) -> ProcessingStageState | None:
    for state in reversed(ledger.states(stage=stage)):
        if (
            state.status != "completed"
            or state.input_artifacts != input_artifacts
            or len(state.output_artifacts) != 1
        ):
            continue
        output_ref = state.output_artifacts[0]
        if not _artifact_ref_verifies(store=store, artifact_ref=output_ref):
            continue
        record = _record_for_ref(store=store, artifact_ref=output_ref)
        if record.metadata.get("policy_config_id") == policy_config_id:
            return state
    return None


def _load_reusable_output(
    *,
    store: ArtifactStore,
    completed: ProcessingStageState,
) -> tuple[ArtifactRecord, ReviewQueueArtifact] | None:
    output_ref = completed.output_artifacts[0]
    try:
        record = _record_for_ref(store=store, artifact_ref=output_ref)
        artifact = ReviewQueueArtifact.from_dict(json.loads(store.retrieve(record.id)))
    except (
        SemanticCoherenceError,
        ReviewFlaggingError,
        json.JSONDecodeError,
        KeyError,
        UnicodeDecodeError,
    ):
        return None
    return record, artifact


def _record_for_ref(*, store: ArtifactStore, artifact_ref: ArtifactRef) -> ArtifactRecord:
    for record in store.list(artifact_type=artifact_ref.artifact_type):
        if (
            record.id == artifact_ref.id
            and record.version == artifact_ref.version
            and record.sha256 == artifact_ref.sha256
        ):
            return record
    raise SemanticCoherenceContractError("Review queue output ref does not match a known artifact.")


def _artifact_ref_verifies(*, store: ArtifactStore, artifact_ref: ArtifactRef) -> bool:
    try:
        record = _record_for_ref(store=store, artifact_ref=artifact_ref)
        verification = store.verify(record.id)
    except (KeyError, SemanticCoherenceContractError):
        return False
    return verification.ok


def _mark_failed(
    *,
    ledger: ProcessingStageLedger,
    stage: str,
    input_ref: ArtifactRef,
    attempt: int,
    started_at: str | None,
    now: datetime | None,
    message: str,
    retryable: bool,
    code: str,
) -> ProcessingStageState:
    return ledger.mark_failed(
        stage=stage,
        input_artifacts=(input_ref,),
        error=ProcessingErrorInfo(message=message, retryable=retryable, code=code),
        attempt=attempt,
        started_at=started_at,
        now=now,
    )


def _review_queue_artifact_name(
    *,
    run_id: str,
    merged_transcript_sha256: str,
    policy_config_id: str,
    attempt: int,
) -> Path:
    scope = sha256(policy_config_id.encode("utf-8")).hexdigest()[:12]
    return Path(run_id) / merged_transcript_sha256[:12] / scope / f"semantic-{attempt:03d}.json"


def _prompt(request: SemanticCoherenceRequest) -> str:
    return json.dumps(
        {
            "task": "semantic_coherence_check",
            "segment": _segment_prompt_payload(request.segment),
            "previous_segments": [
                _segment_prompt_payload(segment) for segment in request.previous_segments
            ],
            "next_segments": [
                _segment_prompt_payload(segment) for segment in request.next_segments
            ],
            "instructions": {
                "coherent": "true when the segment makes sense in the surrounding context",
                "incoherent": (
                    "false when the segment is fluent or plausible text but contextually "
                    "nonsensical, topic-breaking, or likely a transcription error"
                ),
                "language": "Do not rely on language-specific dialect detection.",
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _segment_prompt_payload(segment: MergedTranscriptSegment) -> dict[str, object]:
    return {
        "id": segment.id,
        "speaker": segment.speaker.label,
        "start_seconds": segment.start_seconds,
        "end_seconds": segment.end_seconds,
        "text": segment.text,
    }


def _ollama_message_content(response: Mapping[str, object]) -> str:
    message = response.get("message")
    if not isinstance(message, Mapping):
        raise SemanticCoherenceContractError("Ollama response missing message object.")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise SemanticCoherenceContractError("Ollama response message content is empty.")
    return content


def _strip_json_fence(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```json") and stripped.endswith("```"):
        return stripped.removeprefix("```json").removesuffix("```").strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        return stripped.removeprefix("```").removesuffix("```").strip()
    return stripped


def _local_ollama_endpoint(endpoint: str) -> str:
    sanitized = endpoint.rstrip("/")
    parsed = urllib.parse.urlsplit(sanitized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SemanticCoherenceContractError("Ollama endpoint must be an HTTP URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SemanticCoherenceContractError(
            "Ollama endpoint must not contain credentials, query, or fragment."
        )
    hostname = parsed.hostname
    if hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise SemanticCoherenceContractError("Ollama endpoint must be local.")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _mapping(data: object, label: str) -> Mapping[str, object]:
    if not isinstance(data, Mapping):
        raise SemanticCoherenceContractError(f"{label} must be a JSON object.")
    return data


def _required_str(data: Mapping[str, object], field_name: str, label: str) -> str:
    value = data.get(field_name)
    if not isinstance(value, str) or not value:
        raise SemanticCoherenceContractError(
            f"Field '{field_name}' in {label} must be a non-empty string."
        )
    return value


def _required_int(data: Mapping[str, object], field_name: str, label: str) -> int:
    value = data.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SemanticCoherenceContractError(f"Field '{field_name}' in {label} must be an integer.")
    return value


def _required_number(data: Mapping[str, object], field_name: str, label: str) -> float:
    value = data.get(field_name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SemanticCoherenceContractError(f"Field '{field_name}' in {label} must be a number.")
    return float(value)


def _required_bool(data: Mapping[str, object], field_name: str, label: str) -> bool:
    value = data.get(field_name)
    if not isinstance(value, bool):
        raise SemanticCoherenceContractError(f"Field '{field_name}' in {label} must be a boolean.")
    return value


def _optional_json_object(
    data: Mapping[str, object],
    field_name: str,
    label: str,
) -> Mapping[str, object]:
    value = data.get(field_name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SemanticCoherenceContractError(f"Field '{field_name}' in {label} must be an object.")
    return value


def _validate_probability(value: float, label: str) -> None:
    if not 0.0 <= value <= 1.0:
        raise SemanticCoherenceContractError(f"{label} must be within [0, 1].")


def _timestamp(now: datetime | None) -> str:
    current = now or datetime.now(UTC)
    return current.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
