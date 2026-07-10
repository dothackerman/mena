"""Summarization provider port, summary artifact contract, and stage persistence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol, Self, runtime_checkable

from mena.artifacts import ArtifactRecord, ArtifactStore
from mena.correction_overlay import (
    CORRECTION_OVERLAY_ARTIFACT_TYPE,
    CorrectionOverlayArtifact,
    CorrectionOverlayContractError,
    apply_correction_overlay,
)
from mena.fields import optional_str, required_int, required_str
from mena.local_llm import LocalLLMProviderMetadata
from mena.merge_timing import MergedTranscriptArtifact, MergeTimingContractError
from mena.note_package import (
    ACTION_ITEMS_RESPONSE_SCHEMA,
    DEFAULT_LANGUAGE,
    GENERAL_SUMMARY_RESPONSE_SCHEMA,
    LANGUAGE_PROMPT_POLICY,
    LEGACY_PROMPT_POLICY,
    PRODUCT_RESPONSE_SCHEMA_NAMES,
    PRODUCT_RESPONSE_SCHEMAS,
    PROMPT_POLICY,
    REQUIRED_NOTE_PRODUCTS,
    SCHEMA_PROMPT_POLICY,
    SUMMARIZATION_POLICY_VERSION,
    SUPPORTED_PROMPT_POLICIES,
    TIMELINE_RESPONSE_SCHEMA,
    ProductRequestRecord,
    SummarizationContractError,
    SummarizationError,
    SummarizationPolicy,
    SummarizationRequest,
    SummarizationTokenBudgetError,
    SummaryActionItem,
    SummaryResult,
    SummaryTimelineEntry,
    assemble_summary_result,
    assert_summary_output_language,
    guard_summarization_token_budget,
    product_system_prompt,
    product_user_prompt,
    request_records_from_payload,
    validate_product_payload,
)
from mena.processing import (
    ArtifactRef,
    ProcessingErrorInfo,
    ProcessingStageLedger,
    ProcessingStageState,
)
from mena.timestamps import utc_timestamp

SUMMARY_ARTIFACT_TYPE = "summary"
SUMMARY_ARTIFACT_VERSION = 3
SUMMARY_SCHEMA_VERSION = 3
SUMMARY_ARTIFACT_CONTRACT = f"summary.v{SUMMARY_SCHEMA_VERSION}"
MANUAL_NOTES_ARTIFACT_TYPE = "manual_notes"
MANUAL_NOTES_CONTRACT = "manual-notes.raw"
SUMMARIZATION_STAGE = "summarization"

__all__ = [
    "ACTION_ITEMS_RESPONSE_SCHEMA",
    "DEFAULT_LANGUAGE",
    "GENERAL_SUMMARY_RESPONSE_SCHEMA",
    "LANGUAGE_PROMPT_POLICY",
    "LEGACY_PROMPT_POLICY",
    "MANUAL_NOTES_ARTIFACT_TYPE",
    "MANUAL_NOTES_CONTRACT",
    "PRODUCT_RESPONSE_SCHEMA_NAMES",
    "PRODUCT_RESPONSE_SCHEMAS",
    "PROMPT_POLICY",
    "ProductRequestRecord",
    "REQUIRED_NOTE_PRODUCTS",
    "SCHEMA_PROMPT_POLICY",
    "SUMMARY_ARTIFACT_TYPE",
    "SUMMARY_ARTIFACT_VERSION",
    "SUMMARY_ARTIFACT_CONTRACT",
    "SUMMARY_SCHEMA_VERSION",
    "SUMMARIZATION_POLICY_VERSION",
    "SUMMARIZATION_STAGE",
    "SUPPORTED_PROMPT_POLICIES",
    "SummarizationContractError",
    "SummarizationError",
    "SummarizationPolicy",
    "SummarizationProvider",
    "SummarizationProviderMetadata",
    "SummarizationProviderUnavailable",
    "SummarizationRequest",
    "SummarizationStageResult",
    "SummarizationTokenBudgetError",
    "SummaryActionItem",
    "SummaryArtifact",
    "SummaryResult",
    "SummaryTimelineEntry",
    "TIMELINE_RESPONSE_SCHEMA",
    "assemble_summary_result",
    "assert_summary_output_language",
    "guard_summarization_token_budget",
    "persist_summary_artifact",
    "product_system_prompt",
    "product_user_prompt",
    "run_summarization_stage",
    "validate_product_payload",
]


class SummarizationProviderUnavailable(SummarizationError):
    """Raised when a summarization provider cannot be reached."""


@dataclass(frozen=True)
class SummarizationProviderMetadata(LocalLLMProviderMetadata):
    """Provider identity persisted with summary artifacts.

    The effective request-discipline options (``num_ctx``/``temperature``/
    ``seed``) participate in ``config_id`` so that reuse identity (and thus
    ``skip_completed``) invalidates a completed summary when a provider is
    retuned to different request discipline. They are part of the reuse key
    only; the persisted artifact records the effective options per product in
    ``request_records``, so they are intentionally omitted from ``to_dict`` to
    avoid duplicating provenance in the provider block.
    """

    endpoint: str | None = None
    prompt_policy: str = PROMPT_POLICY
    num_ctx: int | None = None
    temperature: float | None = None
    seed: int | None = None
    resolved_model: str | None = None
    account_scope: str | None = None

    @property
    def config_id(self) -> str:
        payload = self.to_dict()
        if any(option is not None for option in (self.num_ctx, self.temperature, self.seed)):
            payload = {
                **payload,
                "request_options": {
                    "num_ctx": self.num_ctx,
                    "temperature": self.temperature,
                    "seed": self.seed,
                },
            }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def to_dict(self) -> dict[str, object]:
        payload = super().to_dict()
        if self.resolved_model is not None:
            payload["resolved_model"] = self.resolved_model
        if self.account_scope is not None:
            payload["account_scope"] = self.account_scope
        return payload


@runtime_checkable
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


@dataclass(frozen=True)
class SummaryArtifact:
    """Persisted `summary.v3` three-product note-package artifact."""

    version: int
    session_id: str
    merged_transcript_artifact: ArtifactRef
    manual_notes_artifact: ArtifactRef | None
    policy: Mapping[str, object]
    provider: SummarizationProviderMetadata
    timeline: tuple[SummaryTimelineEntry, ...]
    action_items: tuple[SummaryActionItem, ...]
    general_summary: str
    created_at: str
    request_records: tuple[ProductRequestRecord, ...] = ()

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
            timeline=tuple(
                SummaryTimelineEntry.from_dict(entry) for entry in _list(item, "timeline")
            ),
            action_items=tuple(
                SummaryActionItem.from_dict(action) for action in _list(item, "action_items")
            ),
            general_summary=_required_str(item, "general_summary", "Summary artifact"),
            created_at=_required_str(item, "created_at", "Summary artifact"),
            request_records=request_records_from_payload(item, required=True),
        )
        if artifact.version != SUMMARY_SCHEMA_VERSION:
            raise SummarizationContractError("Unsupported summary artifact version.")
        if not artifact.timeline:
            raise SummarizationContractError("Summary artifact timeline must not be empty.")
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
            "timeline": [entry.to_dict() for entry in self.timeline],
            "action_items": [action.to_dict() for action in self.action_items],
            "general_summary": self.general_summary,
            "created_at": self.created_at,
            "request_records": [record.to_dict() for record in self.request_records],
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
    correction_overlay_record: ArtifactRecord | None = None,
) -> ArtifactRecord:
    """Persist one summary payload through the artifact store contract."""

    SummaryArtifact.from_dict(artifact.to_dict())
    metadata: dict[str, object] = {
        "category": "summary",
        "contract": SUMMARY_ARTIFACT_CONTRACT,
        "note_product_strategies": _note_product_strategies_metadata(artifact.request_records),
        "policy_config_id": _policy_config_id(artifact.policy),
        "provider_config_id": artifact.provider.config_id,
        "stage_config_id": _stage_config_id(
            policy_config_id=_policy_config_id(artifact.policy),
            provider_config_id=artifact.provider.config_id,
        ),
        "merged_transcript_artifact_id": artifact.merged_transcript_artifact.id,
        "manual_notes_artifact_id": (
            None if artifact.manual_notes_artifact is None else artifact.manual_notes_artifact.id
        ),
    }
    if correction_overlay_record is not None:
        metadata["correction_overlay_artifact_id"] = correction_overlay_record.id
    return store.store(
        artifact_type=SUMMARY_ARTIFACT_TYPE,
        name=name,
        data=artifact.to_json_bytes(),
        version=SUMMARY_ARTIFACT_VERSION,
        metadata=metadata,
    )


def run_summarization_stage(
    *,
    store: ArtifactStore,
    ledger: ProcessingStageLedger,
    merged_transcript_record: ArtifactRecord,
    provider: SummarizationProvider,
    correction_overlay_record: ArtifactRecord | None = None,
    policy: SummarizationPolicy | None = None,
    manual_notes_record: ArtifactRecord | None = None,
    stage: str = SUMMARIZATION_STAGE,
    attempt: int = 1,
    skip_completed: bool = True,
    now: datetime | None = None,
) -> SummarizationStageResult:
    """Generate a structured meeting summary from a merged transcript artifact."""

    effective_policy = policy or SummarizationPolicy()
    correction_overlay_used_record = correction_overlay_record
    manual_notes_used_record = (
        manual_notes_record if effective_policy.include_manual_notes else None
    )
    input_refs = [ArtifactRef.from_record(merged_transcript_record)]
    if correction_overlay_used_record is not None:
        input_refs.append(ArtifactRef.from_record(correction_overlay_used_record))
    if manual_notes_used_record is not None:
        input_refs.append(ArtifactRef.from_record(manual_notes_used_record))
    input_artifacts = tuple(input_refs)
    policy_payload = effective_policy.to_dict()
    policy_config_id = effective_policy.config_id
    stage_config_id = _stage_config_id(
        policy_config_id=policy_config_id,
        provider_config_id=provider.metadata.config_id,
    )

    if skip_completed:
        completed = _completed_state_for_inputs(
            store=store,
            ledger=ledger,
            stage=stage,
            input_artifacts=input_artifacts,
            policy_config_id=policy_config_id,
            provider_config_id=provider.metadata.config_id,
            stage_config_id=stage_config_id,
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

        transcript_for_summary = merged_transcript
        if correction_overlay_used_record is not None:
            overlay = _verify_and_load_correction_overlay(
                store=store, record=correction_overlay_used_record
            )
            transcript_for_summary = apply_correction_overlay(
                source_transcript=merged_transcript,
                source_transcript_record=merged_transcript_record,
                overlay=overlay,
            )

        manual_notes = None
        if manual_notes_used_record is not None:
            manual_notes = _verify_and_load_manual_notes(
                store=store, record=manual_notes_used_record
            )
        result = provider.summarize(
            SummarizationRequest(
                session_id=merged_transcript.session_id,
                merged_transcript=transcript_for_summary,
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
            action_items=result.action_items,
            general_summary=result.general_summary,
            created_at=_timestamp(now),
            request_records=result.request_records,
        )
        output_record = persist_summary_artifact(
            store=store,
            artifact=artifact,
            correction_overlay_record=correction_overlay_used_record,
            name=_summary_artifact_name(
                run_id=ledger.run_id,
                merged_transcript_sha256=merged_transcript_record.sha256,
                stage_config_id=stage_config_id,
                manual_notes_sha256=(
                    None if manual_notes_used_record is None else manual_notes_used_record.sha256
                ),
                correction_overlay_sha256=(
                    None
                    if correction_overlay_used_record is None
                    else correction_overlay_used_record.sha256
                ),
                correction_overlay_artifact_id=(
                    None
                    if correction_overlay_used_record is None
                    else correction_overlay_used_record.id
                ),
                attempt=attempt,
            ),
        )
    except (
        SummarizationError,
        MergeTimingContractError,
        CorrectionOverlayContractError,
        json.JSONDecodeError,
        KeyError,
        FileExistsError,
        UnicodeDecodeError,
    ) as exc:
        failed = ledger.mark_failed(
            stage=stage,
            input_artifacts=input_artifacts,
            error=ProcessingErrorInfo(
                message=_processing_error_message(exc),
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


def _verify_and_load_correction_overlay(
    *, store: ArtifactStore, record: ArtifactRecord
) -> CorrectionOverlayArtifact:
    if record.artifact_type != CORRECTION_OVERLAY_ARTIFACT_TYPE:
        raise SummarizationContractError(
            "Correction overlay artifact must have correction_overlay type."
        )
    if not store.verify(record.id).ok:
        raise SummarizationContractError("Correction overlay artifact failed verification.")
    return CorrectionOverlayArtifact.from_dict(json.loads(store.retrieve(record.id)))


def _verify_and_load_merged_transcript(
    *, store: ArtifactStore, record: ArtifactRecord
) -> MergedTranscriptArtifact:
    verification = store.verify(record.id)
    if not verification.ok:
        raise SummarizationContractError(
            verification.error or "merged transcript artifact failed verification"
        )
    return MergedTranscriptArtifact.from_dict(json.loads(store.retrieve(record.id)))


def _verify_and_load_manual_notes(*, store: ArtifactStore, record: ArtifactRecord) -> str:
    if record.artifact_type != MANUAL_NOTES_ARTIFACT_TYPE:
        raise SummarizationContractError("Manual notes artifact must have manual_notes type.")
    if record.metadata.get("contract") != MANUAL_NOTES_CONTRACT:
        raise SummarizationContractError("Manual notes artifact has unsupported contract.")
    if not store.verify(record.id).ok:
        raise SummarizationContractError("Manual notes artifact failed verification.")
    return store.retrieve(record.id).decode("utf-8")


def _completed_state_for_inputs(
    *,
    store: ArtifactStore,
    ledger: ProcessingStageLedger,
    stage: str,
    input_artifacts: tuple[ArtifactRef, ...],
    policy_config_id: str,
    provider_config_id: str,
    stage_config_id: str,
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
        if record.metadata.get("stage_config_id") == stage_config_id or (
            record.metadata.get("policy_config_id") == policy_config_id
            and record.metadata.get("provider_config_id") == provider_config_id
        ):
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
    stage_config_id: str,
    manual_notes_sha256: str | None,
    correction_overlay_sha256: str | None,
    correction_overlay_artifact_id: str | None,
    attempt: int,
) -> Path:
    notes_scope = manual_notes_sha256 or "no-manual-notes"
    overlay_scope = (
        f"{correction_overlay_artifact_id}:{correction_overlay_sha256}"
        if correction_overlay_artifact_id is not None
        else "no-correction-overlay"
    )
    scope = sha256(f"{stage_config_id}:{notes_scope}:{overlay_scope}".encode()).hexdigest()[:12]
    return Path(run_id) / merged_transcript_sha256[:12] / scope / f"summary-{attempt:03d}.json"


def _processing_error_message(exc: BaseException) -> str:
    message = str(exc)
    if isinstance(exc, SummarizationContractError):
        message = message.split(" Raw Ollama message content:", 1)[0]
        message = message.split(" Raw provider message content:", 1)[0]
    return message


def _provider_from_dict(data: object) -> SummarizationProviderMetadata:
    item = _mapping(data, "Summary provider")
    provider = SummarizationProviderMetadata(
        name=_required_str(item, "name", "Summary provider"),
        model=_required_str(item, "model", "Summary provider"),
        endpoint=_optional_str(item, "endpoint", "Summary provider"),
        prompt_policy=_required_str(item, "prompt_policy", "Summary provider"),
        resolved_model=_optional_str(item, "resolved_model", "Summary provider"),
        account_scope=_optional_str(item, "account_scope", "Summary provider"),
    )
    if provider.prompt_policy not in SUPPORTED_PROMPT_POLICIES:
        raise SummarizationContractError("Unsupported summary provider prompt policy.")
    return provider


def _summary_policy_payload(data: object) -> dict[str, object]:
    policy = _mapping(data, "Summary policy")
    required = {
        "policy_version",
        "prompt_policy",
        "language",
        "include_manual_notes",
    }
    if set(policy) != required:
        raise SummarizationContractError("Summary policy has unsupported fields.")
    SummarizationPolicy.from_dict(policy)
    return dict(policy)


def _policy_config_id(policy: Mapping[str, object]) -> str:
    return json.dumps(dict(policy), sort_keys=True, separators=(",", ":"))


def _stage_config_id(*, policy_config_id: str, provider_config_id: str) -> str:
    return json.dumps(
        {
            "policy_config_id": policy_config_id,
            "provider_config_id": provider_config_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _note_product_strategies_metadata(records: tuple[ProductRequestRecord, ...]) -> str:
    strategies = {record.product: record.strategy for record in records}
    return ",".join(
        f"{product}={strategies[product]}"
        for product in REQUIRED_NOTE_PRODUCTS
        if product in strategies
    )


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
    return required_str(
        data,
        field_name,
        error=SummarizationContractError,
        message=f"Field '{field_name}' in {label} must be a non-empty string.",
    )


def _optional_str(data: Mapping[str, object], field_name: str, label: str) -> str | None:
    return optional_str(
        data,
        field_name,
        error=SummarizationContractError,
        message=f"Field '{field_name}' in {label} must be null or a non-empty string.",
    )


def _required_int(data: Mapping[str, object], field_name: str, label: str) -> int:
    return required_int(
        data,
        field_name,
        error=SummarizationContractError,
        message=f"Field '{field_name}' in {label} must be an integer.",
    )


def _timestamp(now: datetime | None) -> str:
    return utc_timestamp(now or datetime.now(UTC))
