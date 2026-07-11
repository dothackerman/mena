"""Runtime resource stewardship contracts and provenance."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from mena.artifacts import ArtifactRecord, ArtifactStore

RUNTIME_STEWARDSHIP_PROVENANCE_CONTRACT = "runtime-stewardship-provenance.v1"
RUNTIME_STEWARDSHIP_RELEASE_NOTICE_TOPIC = "runtime_stewardship.release"
RUNTIME_RELEASE_STATUSES = frozenset({"released", "not_supported", "failed"})
RUNTIME_RELEASE_UNOBSERVED_RESOURCE_DETAIL = (
    "Provider did not observe Mena-owned runtime resource use for this lease."
)


@dataclass(frozen=True)
class RuntimeLease:
    """A Mena-owned runtime resource lease at an application boundary."""

    resource_kind: str
    stage: str
    boundary: str
    provider: str
    model: str
    ownership: str = "mena-owned"

    def to_dict(self) -> dict[str, object]:
        return {
            "resource_kind": self.resource_kind,
            "stage": self.stage,
            "boundary": self.boundary,
            "provider": self.provider,
            "model": self.model,
            "ownership": self.ownership,
        }


@dataclass(frozen=True)
class RuntimeReleaseResult:
    """Provider-independent result of releasing one runtime lease."""

    lease: RuntimeLease
    status: str
    detail: str

    def __post_init__(self) -> None:
        if self.status not in RUNTIME_RELEASE_STATUSES:
            valid = ", ".join(sorted(RUNTIME_RELEASE_STATUSES))
            raise ValueError(f"Runtime release status must be one of: {valid}.")
        if not self.detail:
            raise ValueError("Runtime release detail must be a non-empty string.")

    def to_dict(self) -> dict[str, object]:
        return {
            "lease": self.lease.to_dict(),
            "status": self.status,
            "detail": self.detail,
        }


class ReleasableRuntime(Protocol):
    """Provider-side capability for releasing a Mena-owned runtime lease."""

    metadata: Any

    def release_runtime_resource(self, lease: RuntimeLease) -> RuntimeReleaseResult:
        """Release the provider-specific resource represented by `lease`."""


def local_llm_lease(*, provider: object, stage: str, boundary: str) -> RuntimeLease:
    """Create a generic local LLM lease from provider metadata."""

    metadata = getattr(provider, "metadata", None)
    provider_name = getattr(metadata, "name", "unknown")
    model = getattr(metadata, "model", "unknown")
    return RuntimeLease(
        resource_kind="local_llm",
        stage=stage,
        boundary=boundary,
        provider=str(provider_name),
        model=str(model),
    )


def release_terminal_local_llm(
    *,
    provider: object,
    stage: str,
    boundary: str,
) -> RuntimeReleaseResult:
    """Ask a provider to release a terminal local LLM lease without knowing provider mechanics."""

    lease = local_llm_lease(provider=provider, stage=stage, boundary=boundary)
    if getattr(provider, "runtime_resource_used", False) is False:
        return RuntimeReleaseResult(
            lease=lease,
            status="not_supported",
            detail=RUNTIME_RELEASE_UNOBSERVED_RESOURCE_DETAIL,
        )
    release = getattr(provider, "release_runtime_resource", None)
    if not callable(release):
        return RuntimeReleaseResult(
            lease=lease,
            status="not_supported",
            detail="Provider does not expose a runtime release capability.",
        )
    try:
        result = release(lease)
    except Exception as exc:  # noqa: BLE001 - release failures must be provenance-backed.
        return RuntimeReleaseResult(
            lease=lease,
            status="failed",
            detail=str(exc) or exc.__class__.__name__,
        )
    if not isinstance(result, RuntimeReleaseResult):
        return RuntimeReleaseResult(
            lease=lease,
            status="failed",
            detail="Provider runtime release returned an invalid result.",
        )
    return result


def record_terminal_runtime_release(
    *,
    store: ArtifactStore,
    run_id: str,
    attempt: int,
    provider: object,
    stage: str,
    boundary: str,
) -> RuntimeReleaseResult | None:
    """Release and persist terminal runtime stewardship, skipping unused leases."""

    result = release_terminal_local_llm(provider=provider, stage=stage, boundary=boundary)
    if _release_skipped_without_observed_resource_use(result):
        return None
    store_runtime_stewardship_provenance(
        store=store,
        run_id=run_id,
        attempt=attempt,
        boundary=boundary,
        release_results=(result,),
    )
    return result


def store_runtime_stewardship_provenance(
    *,
    store: ArtifactStore,
    run_id: str,
    attempt: int,
    boundary: str,
    release_results: Sequence[RuntimeReleaseResult],
) -> ArtifactRecord:
    """Persist runtime stewardship outcomes for a command attempt."""

    payload = {
        "contract": RUNTIME_STEWARDSHIP_PROVENANCE_CONTRACT,
        "run_id": run_id,
        "attempt": attempt,
        "boundary": boundary,
        "releases": [result.to_dict() for result in release_results],
    }
    status = _aggregate_status(release_results)
    return store.store(
        artifact_type="runtime_stewardship_provenance",
        name=f"{run_id}/attempt-{attempt}/runtime-stewardship-{boundary}.json",
        data=(json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        version=1,
        metadata={
            "contract": RUNTIME_STEWARDSHIP_PROVENANCE_CONTRACT,
            "run_id": run_id,
            "attempt": attempt,
            "boundary": boundary,
            "status": status,
        },
    )


def _aggregate_status(release_results: Sequence[RuntimeReleaseResult]) -> str:
    if any(result.status == "failed" for result in release_results):
        return "failed"
    if any(result.status == "released" for result in release_results):
        return "released"
    return "not_supported"


def _release_skipped_without_observed_resource_use(result: RuntimeReleaseResult) -> bool:
    return (
        result.status == "not_supported"
        and result.detail == RUNTIME_RELEASE_UNOBSERVED_RESOURCE_DETAIL
    )
