from __future__ import annotations

import json

from mena.artifacts import InMemoryArtifactStore
from mena.runtime_stewardship import (
    RuntimeLease,
    RuntimeReleaseResult,
    local_llm_lease,
    record_terminal_runtime_release,
    release_terminal_local_llm,
    store_runtime_stewardship_provenance,
)


def test_release_terminal_local_llm_delegates_to_provider_capability() -> None:
    class Provider:
        metadata = type("Metadata", (), {"name": "fake-llm", "model": "model-a"})()
        runtime_resource_used = True

        def __init__(self) -> None:
            self.lease: RuntimeLease | None = None

        def release_runtime_resource(self, lease: RuntimeLease) -> RuntimeReleaseResult:
            self.lease = lease
            return RuntimeReleaseResult(
                lease=lease,
                status="released",
                detail="released by fake provider",
            )

    provider = Provider()

    result = release_terminal_local_llm(
        provider=provider,
        stage="summarization",
        boundary="summarize",
    )

    assert result.status == "released"
    assert provider.lease == result.lease
    assert result.lease.resource_kind == "local_llm"
    assert result.lease.provider == "fake-llm"
    assert result.lease.model == "model-a"


def test_release_terminal_local_llm_records_missing_or_failed_release_capability() -> None:
    class NoReleaseProvider:
        metadata = type("Metadata", (), {"name": "fake-llm", "model": "model-a"})()
        runtime_resource_used = True

    class FailingProvider:
        metadata = type("Metadata", (), {"name": "fake-llm", "model": "model-a"})()
        runtime_resource_used = True

        def release_runtime_resource(self, lease: RuntimeLease) -> RuntimeReleaseResult:
            raise RuntimeError("release failed")

    missing = release_terminal_local_llm(
        provider=NoReleaseProvider(),
        stage="semantic-review",
        boundary="process",
    )
    failed = release_terminal_local_llm(
        provider=FailingProvider(),
        stage="semantic-review",
        boundary="process",
    )

    assert missing.status == "not_supported"
    assert failed.status == "failed"
    assert failed.detail == "release failed"


def test_release_terminal_local_llm_skips_provider_without_observed_resource_use() -> None:
    class Provider:
        metadata = type("Metadata", (), {"name": "fake-llm", "model": "model-a"})()
        runtime_resource_used = False

        def release_runtime_resource(self, lease: RuntimeLease) -> RuntimeReleaseResult:
            raise AssertionError("release should not run when no runtime resource was used")

    result = release_terminal_local_llm(
        provider=Provider(),
        stage="summarization",
        boundary="summarize",
    )

    assert result.status == "not_supported"
    assert "did not observe Mena-owned runtime resource use" in result.detail


def test_runtime_stewardship_provenance_persists_release_results() -> None:
    store = InMemoryArtifactStore(session_id="session-1")
    released = RuntimeReleaseResult(
        lease=local_llm_lease(
            provider=type(
                "Provider",
                (),
                {"metadata": type("Metadata", (), {"name": "ollama", "model": "llama3.2:3b"})()},
            )(),
            stage="summarization",
            boundary="summarize",
        ),
        status="released",
        detail="Ollama model release completed: unload.",
    )

    record = store_runtime_stewardship_provenance(
        store=store,
        run_id="summarize-1",
        attempt=1,
        boundary="summarize",
        release_results=(released,),
    )
    payload = json.loads(store.retrieve(record.id))

    assert payload["contract"] == "runtime-stewardship-provenance.v1"
    assert payload["boundary"] == "summarize"
    assert payload["releases"][0]["status"] == "released"
    assert payload["releases"][0]["lease"]["resource_kind"] == "local_llm"
    assert record.metadata["status"] == "released"


def test_record_terminal_runtime_release_skips_unobserved_resource_use() -> None:
    class Provider:
        metadata = type("Metadata", (), {"name": "fake-llm", "model": "model-a"})()
        runtime_resource_used = False

    store = InMemoryArtifactStore(session_id="session-1")

    result = record_terminal_runtime_release(
        store=store,
        run_id="summarize-1",
        attempt=1,
        provider=Provider(),
        stage="summarization",
        boundary="summarize",
    )

    assert result is None
    assert store.list(artifact_type="runtime_stewardship_provenance") == ()


def test_record_terminal_runtime_release_persists_observed_release_result() -> None:
    class Provider:
        metadata = type("Metadata", (), {"name": "fake-llm", "model": "model-a"})()
        runtime_resource_used = True

        def release_runtime_resource(self, lease: RuntimeLease) -> RuntimeReleaseResult:
            return RuntimeReleaseResult(
                lease=lease,
                status="released",
                detail="released by fake provider",
            )

    store = InMemoryArtifactStore(session_id="session-1")

    result = record_terminal_runtime_release(
        store=store,
        run_id="summarize-1",
        attempt=1,
        provider=Provider(),
        stage="summarization",
        boundary="summarize",
    )

    records = store.list(artifact_type="runtime_stewardship_provenance")
    assert result is not None
    assert result.status == "released"
    assert len(records) == 1
    assert records[0].metadata["status"] == "released"
