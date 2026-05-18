"""Summarization provider port, summary artifact contract, and local adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol, Self

from mena.artifacts import ArtifactRecord, ArtifactStore
from mena.local_llm import (
    OLLAMA_DEFAULT_ENDPOINT,
    OLLAMA_DEFAULT_MODEL,
    LocalLLMContractError,
    LocalLLMProviderMetadata,
    LocalLLMProviderUnavailable,
    local_ollama_endpoint,
    ollama_message_content,
    post_ollama_json,
    strip_json_fence,
)
from mena.merge_timing import MergedTranscriptArtifact, MergeTimingContractError
from mena.processing import (
    ArtifactRef,
    ProcessingErrorInfo,
    ProcessingStageLedger,
    ProcessingStageState,
)

SUMMARY_ARTIFACT_TYPE = "summary"
SUMMARY_ARTIFACT_VERSION = 1
SUMMARY_SCHEMA_VERSION = 1
SUMMARIZATION_STAGE = "summarization"
SUMMARIZATION_POLICY_VERSION = 1
PROMPT_POLICY = "meeting-summary-json-v1"
DEFAULT_LANGUAGE = "en"


class SummarizationError(RuntimeError):
    """Base class for summarization-stage failures."""


class SummarizationContractError(SummarizationError):
    """Raised when summary payloads violate the v1 contract."""


class SummarizationProviderUnavailable(SummarizationError):
    """Raised when a summarization provider cannot be reached."""


@dataclass(frozen=True)
class SummarizationPolicy:
    """Deterministic policy for meeting summarization."""

    policy_version: int = SUMMARIZATION_POLICY_VERSION
    prompt_policy: str = PROMPT_POLICY
    language: str = DEFAULT_LANGUAGE
    include_manual_notes: bool = True

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Summarization policy")
        policy = cls(
            policy_version=_required_int(item, "policy_version", "Summarization policy"),
            prompt_policy=_required_str(item, "prompt_policy", "Summarization policy"),
            language=_required_str(item, "language", "Summarization policy"),
            include_manual_notes=_required_bool(
                item,
                "include_manual_notes",
                "Summarization policy",
            ),
        )
        if policy.policy_version != SUMMARIZATION_POLICY_VERSION:
            raise SummarizationContractError("Unsupported summarization policy version.")
        if policy.prompt_policy != PROMPT_POLICY:
            raise SummarizationContractError("Unsupported summarization prompt policy.")
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
            "prompt_policy": self.prompt_policy,
            "language": self.language,
            "include_manual_notes": self.include_manual_notes,
            "provider_config_id": provider_config_id,
        }


@dataclass(frozen=True)
class SummarizationProviderMetadata(LocalLLMProviderMetadata):
    """Provider identity persisted with summary artifacts."""

    endpoint: str | None = None
    prompt_policy: str = PROMPT_POLICY


@dataclass(frozen=True)
class SummaryActionItem:
    """One extracted meeting action item."""

    text: str
    owner: str | None = None
    due: str | None = None

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Summary action item")
        return cls(
            text=_required_str(item, "text", "Summary action item"),
            owner=_optional_str(item, "owner", "Summary action item"),
            due=_optional_str(item, "due", "Summary action item"),
        )

    def to_dict(self) -> dict[str, object]:
        return {"text": self.text, "owner": self.owner, "due": self.due}


@dataclass(frozen=True)
class SummaryResult:
    """Provider result for one meeting summary."""

    timeline: tuple[str, ...]
    important_points: tuple[str, ...]
    action_items: tuple[SummaryActionItem, ...]
    general_summary: str

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Summary result")
        result = cls(
            timeline=tuple(_str_list(item, "timeline", "Summary timeline")),
            important_points=tuple(_str_list(item, "important_points", "Summary important points")),
            action_items=tuple(
                SummaryActionItem.from_dict(action) for action in _list(item, "action_items")
            ),
            general_summary=_required_str(item, "general_summary", "Summary result"),
        )
        if not result.timeline:
            raise SummarizationContractError("Summary timeline must not be empty.")
        if not result.important_points:
            raise SummarizationContractError("Summary important_points must not be empty.")
        return result

    def to_dict(self) -> dict[str, object]:
        return {
            "timeline": list(self.timeline),
            "important_points": list(self.important_points),
            "action_items": [action.to_dict() for action in self.action_items],
            "general_summary": self.general_summary,
        }


@dataclass(frozen=True)
class SummarizationRequest:
    """Transcript plus optional manual notes sent to a summarization provider."""

    session_id: str
    merged_transcript: MergedTranscriptArtifact
    manual_notes: str | None
    policy: SummarizationPolicy


class SummarizationProvider(Protocol):
    """Provider port for meeting summarization."""

    metadata: SummarizationProviderMetadata

    def summarize(self, request: SummarizationRequest) -> SummaryResult:
        """Return one structured summary for a merged transcript."""


class FakeSummarizationProvider:
    """Deterministic summarization provider for tests and fixtures."""

    def __init__(self, *, result: SummaryResult, model: str = "fake-summarizer") -> None:
        self.metadata = SummarizationProviderMetadata(name="fake", model=model)
        self.result = result
        self.requests: list[SummarizationRequest] = []

    def summarize(self, request: SummarizationRequest) -> SummaryResult:
        self.requests.append(request)
        return self.result


class OllamaSummarizationProvider:
    """Local Ollama adapter for structured meeting summarization."""

    def __init__(
        self,
        *,
        model: str = OLLAMA_DEFAULT_MODEL,
        endpoint: str = OLLAMA_DEFAULT_ENDPOINT,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.metadata = SummarizationProviderMetadata(
            name="ollama",
            model=model,
            endpoint=_summarization_local_ollama_endpoint(endpoint),
        )
        self.timeout_seconds = timeout_seconds

    def summarize(self, request: SummarizationRequest) -> SummaryResult:
        payload = {
            "model": self.metadata.model,
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You summarize meeting transcripts into strict JSON. Return only keys "
                        "timeline (array of strings), important_points (array of strings), "
                        "action_items (array of objects with text, owner, due), and "
                        "general_summary (string). Be faithful to the transcript and manual notes."
                    ),
                },
                {"role": "user", "content": _prompt(request)},
            ],
        }
        response = self._post_json("/api/chat", payload)
        content = _summarization_ollama_message_content(response)
        try:
            decoded = json.loads(strip_json_fence(content))
        except json.JSONDecodeError as exc:
            raise SummarizationContractError("Ollama summary response was not valid JSON.") from exc
        return SummaryResult.from_dict(decoded)

    def _post_json(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        try:
            return post_ollama_json(
                endpoint=str(self.metadata.endpoint),
                path=path,
                payload=payload,
                timeout_seconds=self.timeout_seconds,
            )
        except LocalLLMProviderUnavailable as exc:
            raise SummarizationProviderUnavailable(
                "Ollama summarization provider is unavailable."
            ) from exc
        except LocalLLMContractError as exc:
            raise SummarizationContractError(str(exc)) from exc


@dataclass(frozen=True)
class SummaryArtifact:
    """Persisted `summary.v1` artifact."""

    version: int
    session_id: str
    merged_transcript_artifact: ArtifactRef
    manual_notes_artifact: ArtifactRef | None
    policy: Mapping[str, object]
    provider: SummarizationProviderMetadata
    timeline: tuple[str, ...]
    important_points: tuple[str, ...]
    action_items: tuple[SummaryActionItem, ...]
    general_summary: str
    created_at: str

    @classmethod
    def from_dict(cls, data: object) -> Self:
        item = _mapping(data, "Summary artifact")
        artifact = cls(
            version=_required_int(item, "version", "Summary artifact"),
            session_id=_required_str(item, "session_id", "Summary artifact"),
            merged_transcript_artifact=ArtifactRef.from_dict(
                item.get("merged_transcript_artifact")
            ),
            manual_notes_artifact=(
                None
                if item.get("manual_notes_artifact") is None
                else ArtifactRef.from_dict(item.get("manual_notes_artifact"))
            ),
            policy=_summary_policy_payload(item.get("policy")),
            provider=_provider_from_dict(item.get("provider")),
            timeline=tuple(_str_list(item, "timeline", "Summary timeline")),
            important_points=tuple(_str_list(item, "important_points", "Summary important points")),
            action_items=tuple(
                SummaryActionItem.from_dict(action) for action in _list(item, "action_items")
            ),
            general_summary=_required_str(item, "general_summary", "Summary artifact"),
            created_at=_required_str(item, "created_at", "Summary artifact"),
        )
        if artifact.version != SUMMARY_SCHEMA_VERSION:
            raise SummarizationContractError("Unsupported summary artifact version.")
        if not artifact.timeline:
            raise SummarizationContractError("Summary artifact timeline must not be empty.")
        if not artifact.important_points:
            raise SummarizationContractError("Summary artifact important_points must not be empty.")
        return artifact

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "session_id": self.session_id,
            "merged_transcript_artifact": self.merged_transcript_artifact.to_dict(),
            "manual_notes_artifact": (
                None if self.manual_notes_artifact is None else self.manual_notes_artifact.to_dict()
            ),
            "policy": dict(self.policy),
            "provider": self.provider.to_dict(),
            "timeline": list(self.timeline),
            "important_points": list(self.important_points),
            "action_items": [action.to_dict() for action in self.action_items],
            "general_summary": self.general_summary,
            "created_at": self.created_at,
        }

    def to_json_bytes(self) -> bytes:
        return (json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")


@dataclass(frozen=True)
class SummarizationStageResult:
    """Result of running or reusing the summarization stage."""

    state: ProcessingStageState
    summary: SummaryArtifact
    summary_record: ArtifactRecord
    skipped: bool = False
    skip_reason: str | None = None


def persist_summary_artifact(
    *,
    store: ArtifactStore,
    artifact: SummaryArtifact,
    name: str | Path,
) -> ArtifactRecord:
    """Persist one summary payload through the artifact store contract."""

    SummaryArtifact.from_dict(artifact.to_dict())
    return store.store(
        artifact_type=SUMMARY_ARTIFACT_TYPE,
        name=name,
        data=artifact.to_json_bytes(),
        version=SUMMARY_ARTIFACT_VERSION,
        metadata={
            "category": "summary",
            "contract": "summary.v1",
            "policy_config_id": _policy_config_id(artifact.policy),
            "provider_config_id": artifact.provider.config_id,
            "merged_transcript_artifact_id": artifact.merged_transcript_artifact.id,
            "manual_notes_artifact_id": (
                None
                if artifact.manual_notes_artifact is None
                else artifact.manual_notes_artifact.id
            ),
        },
    )


def run_summarization_stage(
    *,
    store: ArtifactStore,
    ledger: ProcessingStageLedger,
    merged_transcript_record: ArtifactRecord,
    provider: SummarizationProvider,
    policy: SummarizationPolicy | None = None,
    manual_notes_record: ArtifactRecord | None = None,
    stage: str = SUMMARIZATION_STAGE,
    attempt: int = 1,
    skip_completed: bool = True,
    now: datetime | None = None,
) -> SummarizationStageResult:
    """Generate a structured meeting summary from a merged transcript artifact."""

    effective_policy = policy or SummarizationPolicy()
    manual_notes_used_record = (
        manual_notes_record if effective_policy.include_manual_notes else None
    )
    input_refs = [ArtifactRef.from_record(merged_transcript_record)]
    if manual_notes_used_record is not None:
        input_refs.append(ArtifactRef.from_record(manual_notes_used_record))
    input_artifacts = tuple(input_refs)
    policy_payload = effective_policy.to_dict(provider_config_id=provider.metadata.config_id)
    policy_config_id = effective_policy.config_id(provider_config_id=provider.metadata.config_id)

    if skip_completed:
        completed = _completed_state_for_inputs(
            store=store,
            ledger=ledger,
            stage=stage,
            input_artifacts=input_artifacts,
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
                        input_artifacts=input_artifacts,
                        output_artifacts=completed.output_artifacts,
                        started_at=timestamp,
                        completed_at=timestamp,
                        error=None,
                        attempt=attempt,
                    )
                )
                return SummarizationStageResult(
                    state=state,
                    summary=artifact,
                    summary_record=record,
                    skipped=True,
                    skip_reason="completed_output_reused",
                )

    started = ledger.mark_running(
        stage=stage,
        input_artifacts=input_artifacts,
        attempt=attempt,
        now=now,
    )
    try:
        merged_transcript = _verify_and_load_merged_transcript(
            store=store, record=merged_transcript_record
        )
        if merged_transcript.session_id != ledger.session_id:
            raise SummarizationContractError(
                "Merged transcript session_id must match the processing ledger."
            )
        manual_notes = None
        if manual_notes_used_record is not None:
            if not store.verify(manual_notes_used_record.id).ok:
                raise SummarizationContractError("Manual notes artifact failed verification.")
            manual_notes = store.retrieve(manual_notes_used_record.id).decode("utf-8")
        result = provider.summarize(
            SummarizationRequest(
                session_id=merged_transcript.session_id,
                merged_transcript=merged_transcript,
                manual_notes=manual_notes,
                policy=effective_policy,
            )
        )
        artifact = SummaryArtifact(
            version=SUMMARY_SCHEMA_VERSION,
            session_id=merged_transcript.session_id,
            merged_transcript_artifact=ArtifactRef.from_record(merged_transcript_record),
            manual_notes_artifact=(
                None
                if manual_notes_used_record is None
                else ArtifactRef.from_record(manual_notes_used_record)
            ),
            policy=policy_payload,
            provider=provider.metadata,
            timeline=result.timeline,
            important_points=result.important_points,
            action_items=result.action_items,
            general_summary=result.general_summary,
            created_at=_timestamp(now),
        )
        output_record = persist_summary_artifact(
            store=store,
            artifact=artifact,
            name=_summary_artifact_name(
                run_id=ledger.run_id,
                merged_transcript_sha256=merged_transcript_record.sha256,
                policy_config_id=policy_config_id,
                attempt=attempt,
            ),
        )
    except (
        SummarizationError,
        MergeTimingContractError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        failed = ledger.mark_failed(
            stage=stage,
            input_artifacts=input_artifacts,
            error=ProcessingErrorInfo(
                message=str(exc),
                retryable=isinstance(exc, SummarizationProviderUnavailable),
                code="summarization_failed",
            ),
            attempt=attempt,
            started_at=started.started_at,
            now=now,
        )
        message = failed.error.message if failed.error else str(exc)
        if isinstance(exc, SummarizationProviderUnavailable):
            raise SummarizationProviderUnavailable(message) from exc
        raise SummarizationError(message) from exc

    completed = ledger.mark_completed(
        stage=stage,
        input_artifacts=input_artifacts,
        output_artifacts=(ArtifactRef.from_record(output_record),),
        attempt=attempt,
        started_at=started.started_at,
        now=now,
    )
    return SummarizationStageResult(
        state=completed,
        summary=artifact,
        summary_record=output_record,
    )


def _verify_and_load_merged_transcript(
    *, store: ArtifactStore, record: ArtifactRecord
) -> MergedTranscriptArtifact:
    verification = store.verify(record.id)
    if not verification.ok:
        raise SummarizationContractError(
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
    *, store: ArtifactStore, completed: ProcessingStageState
) -> tuple[ArtifactRecord, SummaryArtifact] | None:
    output_ref = completed.output_artifacts[0]
    try:
        record = _record_for_ref(store=store, artifact_ref=output_ref)
        artifact = SummaryArtifact.from_dict(json.loads(store.retrieve(record.id)))
    except (SummarizationError, json.JSONDecodeError, KeyError, UnicodeDecodeError):
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
    raise SummarizationContractError("Summary output ref does not match a known artifact.")


def _artifact_ref_verifies(*, store: ArtifactStore, artifact_ref: ArtifactRef) -> bool:
    try:
        record = _record_for_ref(store=store, artifact_ref=artifact_ref)
        verification = store.verify(record.id)
    except (KeyError, SummarizationContractError):
        return False
    return verification.ok


def _summary_artifact_name(
    *,
    run_id: str,
    merged_transcript_sha256: str,
    policy_config_id: str,
    attempt: int,
) -> Path:
    scope = sha256(policy_config_id.encode("utf-8")).hexdigest()[:12]
    return Path(run_id) / merged_transcript_sha256[:12] / scope / f"summary-{attempt:03d}.json"


def _prompt(request: SummarizationRequest) -> str:
    return json.dumps(
        {
            "task": "meeting_summary",
            "language": request.policy.language,
            "transcript": {
                "text": request.merged_transcript.text,
                "segments": [
                    {
                        "speaker": segment.speaker.label,
                        "start_seconds": segment.start_seconds,
                        "end_seconds": segment.end_seconds,
                        "text": segment.text,
                    }
                    for segment in request.merged_transcript.segments
                ],
            },
            "manual_notes": request.manual_notes,
            "instructions": {
                "timeline": "Chronological account of the meeting.",
                "important_points": "Key takeaways; weight manual notes when present.",
                "action_items": "Tasks as objects with text, owner, and due fields.",
                "general_summary": "Concise high-level overview.",
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _summarization_ollama_message_content(response: Mapping[str, object]) -> str:
    try:
        return ollama_message_content(response)
    except LocalLLMContractError as exc:
        raise SummarizationContractError(str(exc)) from exc


def _summarization_local_ollama_endpoint(endpoint: str) -> str:
    try:
        return local_ollama_endpoint(endpoint)
    except LocalLLMContractError as exc:
        raise SummarizationContractError(str(exc)) from exc


def _provider_from_dict(data: object) -> SummarizationProviderMetadata:
    item = _mapping(data, "Summary provider")
    provider = SummarizationProviderMetadata(
        name=_required_str(item, "name", "Summary provider"),
        model=_required_str(item, "model", "Summary provider"),
        endpoint=_optional_str(item, "endpoint", "Summary provider"),
        prompt_policy=_required_str(item, "prompt_policy", "Summary provider"),
    )
    if provider.prompt_policy != PROMPT_POLICY:
        raise SummarizationContractError("Unsupported summary provider prompt policy.")
    return provider


def _summary_policy_payload(data: object) -> dict[str, object]:
    policy = _mapping(data, "Summary policy")
    required = {
        "policy_version",
        "prompt_policy",
        "language",
        "include_manual_notes",
        "provider_config_id",
    }
    if set(policy) != required:
        raise SummarizationContractError("Summary policy has unsupported fields.")
    SummarizationPolicy.from_dict(policy)
    _required_str(policy, "provider_config_id", "Summary policy")
    return dict(policy)


def _policy_config_id(policy: Mapping[str, object]) -> str:
    return json.dumps(dict(policy), sort_keys=True, separators=(",", ":"))


def _mapping(data: object, label: str) -> Mapping[str, object]:
    if not isinstance(data, Mapping):
        raise SummarizationContractError(f"{label} must be a JSON object.")
    return data


def _list(data: Mapping[str, object], field_name: str) -> list[object]:
    value = data.get(field_name)
    if not isinstance(value, list):
        raise SummarizationContractError(f"Field '{field_name}' must be an array.")
    return value


def _str_list(data: Mapping[str, object], field_name: str, label: str) -> list[str]:
    values = _list(data, field_name)
    if any(not isinstance(value, str) or not value for value in values):
        raise SummarizationContractError(f"{label} must contain non-empty strings.")
    return [str(value) for value in values]


def _required_str(data: Mapping[str, object], field_name: str, label: str) -> str:
    value = data.get(field_name)
    if not isinstance(value, str) or not value:
        raise SummarizationContractError(
            f"Field '{field_name}' in {label} must be a non-empty string."
        )
    return value


def _optional_str(data: Mapping[str, object], field_name: str, label: str) -> str | None:
    value = data.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise SummarizationContractError(
            f"Field '{field_name}' in {label} must be null or a non-empty string."
        )
    return value


def _required_int(data: Mapping[str, object], field_name: str, label: str) -> int:
    value = data.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SummarizationContractError(f"Field '{field_name}' in {label} must be an integer.")
    return value


def _required_bool(data: Mapping[str, object], field_name: str, label: str) -> bool:
    value = data.get(field_name)
    if not isinstance(value, bool):
        raise SummarizationContractError(f"Field '{field_name}' in {label} must be a boolean.")
    return value


def _timestamp(now: datetime | None) -> str:
    current = now or datetime.now(UTC)
    return current.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
