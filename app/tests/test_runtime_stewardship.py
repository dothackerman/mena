from __future__ import annotations

import json

from mena.artifacts import InMemoryArtifactStore
from mena.runtime_stewardship import (
    RuntimeLease,
    RuntimeReleaseResult,
    local_llm_lease,
    release_terminal_local_llm,
    store_runtime_stewardship_provenance,
)


def test_release_terminal_local_llm_delegates_to_provider_capability() -> None:
    class Provider:
        metadata = type("Metadata", (), {"name": "fake-llm", "model": "model-a"})()

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

    class FailingProvider:
        metadata = type("Metadata", (), {"name": "fake-llm", "model": "model-a"})()

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
