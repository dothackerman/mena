import json
from pathlib import Path

import pytest

from mena.artifacts import ArtifactRecord, FileSystemArtifactStore
from mena.export import (
    SUMMARY_MARKDOWN_EXPORT_ARTIFACT_TYPE,
    SUMMARY_MARKDOWN_EXPORT_ARTIFACT_VERSION,
    SUMMARY_MARKDOWN_EXPORT_CONTRACT,
    SummaryMarkdownExportError,
    export_summary_markdown,
    resolve_summary_markdown_input,
)
from mena.processing import ArtifactRef
from mena.summarization import (
    OLLAMA_SUMMARIZATION_NUM_CTX,
    OLLAMA_SUMMARIZATION_SEED,
    REQUIRED_NOTE_PRODUCTS,
    SUMMARY_SCHEMA_VERSION,
    FakeSummarizationProvider,
    ProductRequestRecord,
    SummarizationPolicy,
    SummaryArtifact,
    SummaryResult,
    persist_summary_artifact,
)

SESSION_ID = "00000000-0000-0000-0000-000000000001"


def test_export_summary_markdown_stores_artifact_with_expected_contract_and_metadata(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    summary_record, summary_artifact = _persist_summary_artifact(
        store=store,
        created_at="2026-05-18T17:00:00Z",
        name="summary/final.json",
    )

    export_result = export_summary_markdown(store=store)
    export_payload = store.retrieve(export_result.export_record.id).decode("utf-8")

    assert export_result.export_record.artifact_type == SUMMARY_MARKDOWN_EXPORT_ARTIFACT_TYPE
    assert export_result.export_record.version == SUMMARY_MARKDOWN_EXPORT_ARTIFACT_VERSION
    assert export_result.export_record.status == "sealed"
    assert f"summary_markdown/{SUMMARY_MARKDOWN_EXPORT_CONTRACT}/" in (
        export_result.export_record.path
    )
    assert summary_record.path.replace("/", "_") in export_result.export_record.path

    assert export_result.export_record.metadata["category"] == "export"
    assert export_result.export_record.metadata["contract"] == SUMMARY_MARKDOWN_EXPORT_CONTRACT
    assert export_result.export_record.metadata["summary_artifact_id"] == summary_record.id
    assert export_result.export_record.metadata["summary_session_id"] == SESSION_ID
    assert export_result.export_record.metadata["summary_created_at"] == summary_artifact.created_at
    assert export_result.export_record.metadata["merged_transcript_artifact_id"] == (
        summary_artifact.merged_transcript_artifact.id
    )
    assert export_result.summary_record == summary_record
    assert export_result.summary == summary_artifact
    assert export_payload.startswith("## Timeline")


def test_resolve_summary_markdown_input_prefers_latest_timestamp(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    old_record, _ = _persist_summary_artifact(
        store=store,
        created_at="2026-05-18T17:00:00Z",
        name="summary/old.json",
    )
    new_record, _ = _persist_summary_artifact(
        store=store,
        created_at="2026-05-20T17:00:00Z",
        name="summary/new.json",
    )

    selected_record, selected_summary = resolve_summary_markdown_input(store=store)

    assert selected_record.id == new_record.id
    assert selected_summary.created_at == "2026-05-20T17:00:00Z"
    assert selected_record.id != old_record.id


def test_resolve_summary_markdown_input_skips_incompatible_payloads(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    valid_record, _ = _persist_summary_artifact(
        store=store,
        created_at="2026-05-18T17:00:00Z",
        name="summary/valid.json",
    )
    store.store(
        artifact_type="summary",
        name="summary/new-invalid.json",
        data=b"not-json",
        version=1,
        status="sealed",
    )

    selected_record, selected_summary = resolve_summary_markdown_input(store=store)

    assert selected_record.id == valid_record.id
    assert selected_summary.created_at == "2026-05-18T17:00:00Z"


def test_resolve_summary_markdown_input_rejects_corrupt_current_payloads(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    _persist_summary_artifact(
        store=store,
        created_at="2026-05-18T17:00:00Z",
        name="summary/valid.json",
    )
    corrupt_record = store.store(
        artifact_type="summary",
        name="summary/new-corrupt-current.json",
        data=b"not-json",
        version=SUMMARY_SCHEMA_VERSION,
        status="sealed",
    )

    with pytest.raises(SummaryMarkdownExportError, match="not valid JSON"):
        resolve_summary_markdown_input(store=store)

    assert corrupt_record.version == SUMMARY_SCHEMA_VERSION


def test_resolve_summary_markdown_input_selects_current_over_sealed_legacy_v1(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    legacy_v1_payload = json.dumps(
        {
            "version": 1,
            "session_id": SESSION_ID,
            "timeline": ["Discussed milestones."],
            "action_items": [],
            "general_summary": "Legacy summary.",
            "created_at": "2026-05-20T17:00:00Z",
        }
    ).encode("utf-8")
    store.store(
        artifact_type="summary",
        name="summary/legacy-v1.json",
        data=legacy_v1_payload,
        version=1,
        status="sealed",
    )
    current_record, _ = _persist_summary_artifact(
        store=store,
        created_at="2026-05-18T17:00:00Z",
        name="summary/rerun.json",
    )

    selected_record, selected_summary = resolve_summary_markdown_input(store=store)

    assert selected_record.id == current_record.id
    assert selected_summary.created_at == "2026-05-18T17:00:00Z"


def test_resolve_summary_markdown_input_skips_sealed_legacy_v2_without_crashing(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    legacy_v2_payload = json.dumps(
        {
            "version": 2,
            "session_id": SESSION_ID,
            "merged_transcript_artifact": {
                "id": f"{SESSION_ID}:artifacts/merged_transcript/merged.json",
                "artifact_type": "merged_transcript",
                "version": 1,
                "sha256": "1" * 64,
            },
            "manual_notes_artifact": None,
            "policy": {
                "policy_version": 1,
                "prompt_policy": "note-package-three-product-v1",
                "language": "en",
                "include_manual_notes": True,
            },
            "provider": {
                "name": "fake",
                "model": "fake-summarizer",
                "endpoint": None,
                "prompt_policy": "note-package-three-product-v1",
            },
            "timeline": ["Discussed milestones."],
            "action_items": [],
            "general_summary": "Legacy v2 summary.",
            "created_at": "2026-05-20T17:00:00Z",
            "request_records": [
                {
                    "product": product,
                    "model": "fake-summarizer",
                    "num_ctx": OLLAMA_SUMMARIZATION_NUM_CTX,
                    "temperature": 0,
                    "seed": OLLAMA_SUMMARIZATION_SEED,
                    "prompt_eval_count": None,
                    "eval_count": None,
                }
                for product in REQUIRED_NOTE_PRODUCTS
            ],
        }
    ).encode("utf-8")
    store.store(
        artifact_type="summary",
        name="summary/legacy-v2.json",
        data=legacy_v2_payload,
        version=2,
        status="sealed",
    )

    with pytest.raises(SummaryMarkdownExportError, match="No valid summary.v3 artifact"):
        resolve_summary_markdown_input(store=store)


def test_resolve_summary_markdown_input_fails_when_only_incompatible_summaries_exist(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    store.store(
        artifact_type="summary",
        name="summary/new-invalid.json",
        data=b"not-json",
        version=1,
        status="sealed",
    )

    with pytest.raises(SummaryMarkdownExportError, match="No valid summary.v3 artifact"):
        resolve_summary_markdown_input(store=store)


def test_resolve_summary_markdown_input_respects_explicit_summary_record_id(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    desired_record, desired_summary = _persist_summary_artifact(
        store=store,
        created_at="2026-05-18T17:00:00Z",
        name="summary/old.json",
    )
    _persist_summary_artifact(
        store=store,
        created_at="2026-05-20T17:00:00Z",
        name="summary/new.json",
    )

    selected_record, selected_summary = resolve_summary_markdown_input(
        store=store,
        summary_record_id=desired_record.id,
    )

    assert selected_record.id == desired_record.id
    assert selected_summary.created_at == desired_summary.created_at


def test_export_summary_markdown_rejects_no_valid_summary_artifact(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    store.store(
        artifact_type="summary",
        name="summary/bad.json",
        data=b"not-json",
        version=1,
        status="sealed",
    )

    with pytest.raises(SummaryMarkdownExportError, match="No valid summary.v3 artifact"):
        export_summary_markdown(store=store)


def test_export_summary_markdown_reuses_existing_matching_export(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    _persist_summary_artifact(
        store=store,
        created_at="2026-05-18T17:00:00Z",
        name="summary/final.json",
    )

    first = export_summary_markdown(store=store)
    second = export_summary_markdown(store=store)

    assert second.export_record == first.export_record
    assert store.list(artifact_type=SUMMARY_MARKDOWN_EXPORT_ARTIFACT_TYPE) == (first.export_record,)


def test_export_summary_markdown_ignores_old_v1_export_for_localized_renderer(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    summary_record, _ = _persist_summary_artifact(
        store=store,
        created_at="2026-05-18T17:00:00Z",
        name="summary/final.json",
        language="de",
    )
    old_export = store.store(
        artifact_type=SUMMARY_MARKDOWN_EXPORT_ARTIFACT_TYPE,
        name=("summary_markdown/2026-05-18T170000+0000-artifacts_summary_summary_final.json.md"),
        data=(
            b"## Timeline\n\n"
            b"- Discussed milestones.\n\n"
            b"## Important points\n\n"
            b"- Release blocked until review.\n\n"
            b"## Action items\n\n"
            b"\n\n"
            b"## General summary\n\n"
            b"The meeting was productive.\n"
        ),
        version=1,
        status="sealed",
        metadata={
            "category": "export",
            "contract": "summary-markdown.v1",
            "summary_artifact_id": summary_record.id,
        },
    )

    export_result = export_summary_markdown(store=store)
    export_payload = store.retrieve(export_result.export_record.id).decode("utf-8")

    assert export_result.export_record.id != old_export.id
    assert export_result.export_record.version == 3
    assert export_result.export_record.metadata["contract"] == SUMMARY_MARKDOWN_EXPORT_CONTRACT
    assert f"summary_markdown/{SUMMARY_MARKDOWN_EXPORT_CONTRACT}/" in (
        export_result.export_record.path
    )
    assert export_payload.startswith("## Zeitverlauf")
    assert {
        record.id for record in store.list(artifact_type=SUMMARY_MARKDOWN_EXPORT_ARTIFACT_TYPE)
    } == {
        old_export.id,
        export_result.export_record.id,
    }


def test_export_summary_markdown_rejects_existing_export_metadata_drift(
    tmp_path: Path,
) -> None:
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    _persist_summary_artifact(
        store=store,
        created_at="2026-05-18T17:00:00Z",
        name="summary/final.json",
    )
    first = export_summary_markdown(store=store)
    manifest_path = tmp_path / "artifacts" / "artifact-manifest.v1.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest["artifacts"]:
        if item["id"] == first.export_record.id:
            item["metadata"]["summary_artifact_id"] = "different"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SummaryMarkdownExportError, match="metadata does not match"):
        export_summary_markdown(store=store)


def _summary_result_for_export() -> SummaryResult:
    return SummaryResult(
        timeline=("Discussed milestones.", "Aligned on deliverables."),
        action_items=(),
        general_summary="The meeting was productive.",
        request_records=_request_records_for_products(REQUIRED_NOTE_PRODUCTS),
    )


def _persist_summary_artifact(
    *,
    store: FileSystemArtifactStore,
    created_at: str,
    name: str,
    language: str = "en",
) -> tuple[ArtifactRecord, SummaryArtifact]:
    summary_result = _summary_result_for_export()
    summary_artifact = SummaryArtifact(
        version=SUMMARY_SCHEMA_VERSION,
        session_id=SESSION_ID,
        merged_transcript_artifact=ArtifactRef(
            id=f"{SESSION_ID}:artifacts/merged_transcript/merged.json",
            artifact_type="merged_transcript",
            version=1,
            sha256="1" * 64,
        ),
        manual_notes_artifact=None,
        policy=SummarizationPolicy(language=language).to_dict(),
        provider=FakeSummarizationProvider(result=summary_result).metadata,
        timeline=summary_result.timeline,
        action_items=summary_result.action_items,
        general_summary=summary_result.general_summary,
        created_at=created_at,
        request_records=summary_result.request_records,
    )
    record = persist_summary_artifact(
        store=store,
        artifact=summary_artifact,
        name=name,
        correction_overlay_record=None,
    )
    return record, summary_artifact


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
