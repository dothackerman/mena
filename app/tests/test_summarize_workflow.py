import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from mena.artifacts import ArtifactStore, FileSystemArtifactStore, InMemoryArtifactStore
from mena.merge_timing import MERGED_TRANSCRIPT_ARTIFACT_TYPE
from mena.runtime_profile import RuntimeProfileSelection
from mena.sessions import resolve_session, session_language_or_default
from mena.settings import AppSettings
from mena.summarization import SummarizationError, SummarizationPolicy
from mena.summarize_workflow import (
    _merged_transcript_timestamp_key,
    _parse_iso_timestamp,
    _resolve_latest_merged_transcript_record,
    run_summarization_command,
)
from test_cli import _build_summarize_session_bundle

SESSION_ID = "00000000-0000-0000-0000-000000000001"


def _runtime_selection(**_kwargs: object) -> RuntimeProfileSelection:
    return cast(RuntimeProfileSelection, SimpleNamespace())


def test_summarize_workflow_resolves_session_directory_and_record_path(
    tmp_path: Path,
) -> None:
    record: dict[str, object] = {"id": SESSION_ID, "language": "de-CH"}
    session_file = tmp_path / "session.json"
    session_file.write_text(json.dumps(record), encoding="utf-8")

    resolved_from_dir = resolve_session(tmp_path)
    resolved_from_file = resolve_session(session_file)
    assert resolved_from_dir.session_dir == tmp_path
    assert resolved_from_dir.record == record
    assert resolved_from_file.session_dir == tmp_path
    assert resolved_from_file.record == record
    assert session_language_or_default(record, "en") == "de-CH"
    assert session_language_or_default({"id": SESSION_ID}, "en") == "en"


def test_summarize_workflow_rejects_invalid_session_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Session path must be"):
        resolve_session(tmp_path / "missing.json")

    bad_session = tmp_path / "session.json"
    bad_session.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="Cannot read session record"):
        resolve_session(bad_session)


def test_summarize_workflow_filters_invalid_merged_transcripts(tmp_path: Path) -> None:
    memory_store = InMemoryArtifactStore(session_id=SESSION_ID)
    invalid = memory_store.store(
        artifact_type=MERGED_TRANSCRIPT_ARTIFACT_TYPE,
        name="merged-invalid.json",
        data=b"{",
        status="sealed",
    )

    assert _parse_iso_timestamp("") is None
    assert _parse_iso_timestamp("not-a-date") is None
    assert _merged_transcript_timestamp_key(store=memory_store, record=invalid) is None
    assert _resolve_latest_merged_transcript_record(store=memory_store) is None

    file_store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=tmp_path)
    corrupt = file_store.store(
        artifact_type=MERGED_TRANSCRIPT_ARTIFACT_TYPE,
        name="merged-corrupt.json",
        data=b"{",
        status="sealed",
    )
    (tmp_path / corrupt.path).write_bytes(b"changed")

    assert _merged_transcript_timestamp_key(store=file_store, record=corrupt) is None


def test_summarize_workflow_applies_active_overlay_and_manual_notes(
    tmp_path: Path,
) -> None:
    session_dir, _, merged_record, overlay_record = _build_summarize_session_bundle(tmp_path)
    notes_path = tmp_path / "notes.md"
    notes_path.write_text("- manual note from workflow", encoding="utf-8")
    captured: dict[str, object] = {}

    def _fake_run_summarization_stage(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        store = cast(ArtifactStore, kwargs["store"])
        return SimpleNamespace(
            summary_record=store.store(
                artifact_type="summary",
                name="summary/final.json",
                data=b"{}",
                version=1,
            ),
        )

    assert (
        run_summarization_command(
            session_path=session_dir,
            manual_notes=str(notes_path),
            language="de-CH",
            settings=AppSettings.from_env(env={}, home=tmp_path),
            runtime_profile_selector_fn=_runtime_selection,
            runtime_profile_store_fn=lambda **_kwargs: None,
            runtime_summarization_binder_fn=lambda **_kwargs: SimpleNamespace(),
            run_summarization_stage_fn=_fake_run_summarization_stage,
        )
        == 0
    )

    assert captured["merged_transcript_record"] == merged_record
    assert captured["correction_overlay_record"] == overlay_record
    assert captured["manual_notes_record"] is not None
    assert cast(SummarizationPolicy, captured["policy"]).language == "de-CH"


def test_summarize_workflow_binds_provider_through_runtime_resolver(
    tmp_path: Path,
) -> None:
    session_dir, _, _, _ = _build_summarize_session_bundle(tmp_path)
    selection = cast(
        RuntimeProfileSelection,
        SimpleNamespace(actual=SimpleNamespace(accepted_profile_id="profile")),
    )
    provider = SimpleNamespace(metadata=SimpleNamespace(config_id="provider"))
    captured: dict[str, object] = {}

    def _bind(**kwargs: object) -> object:
        captured["selection"] = kwargs["selection"]
        captured["settings"] = kwargs["settings"]
        return provider

    def _stage(**kwargs: object) -> SimpleNamespace:
        captured["provider"] = kwargs["provider"]
        store = cast(ArtifactStore, kwargs["store"])
        return SimpleNamespace(
            summary_record=store.store(
                artifact_type="summary",
                name="summary/final.json",
                data=b"{}",
                version=1,
            ),
        )

    settings = AppSettings.from_env(env={}, home=tmp_path)

    assert (
        run_summarization_command(
            session_path=session_dir,
            manual_notes=None,
            language=None,
            settings=settings,
            runtime_profile_selector_fn=lambda **_kwargs: selection,
            runtime_profile_store_fn=lambda **_kwargs: None,
            runtime_summarization_binder_fn=_bind,
            run_summarization_stage_fn=_stage,
        )
        == 0
    )

    assert captured["selection"] is selection
    assert captured["settings"] is settings
    assert captured["provider"] is provider


def test_summarize_workflow_uses_unique_runtime_profile_provenance_per_run(
    tmp_path: Path,
) -> None:
    session_dir, _, _, _ = _build_summarize_session_bundle(tmp_path)
    run_ids: list[str] = []

    def _store(**kwargs: object) -> None:
        run_ids.append(cast(str, kwargs["run_id"]))

    def _stage(**kwargs: object) -> SimpleNamespace:
        store = cast(ArtifactStore, kwargs["store"])
        ledger = cast(SimpleNamespace, kwargs["ledger"])
        return SimpleNamespace(
            summary_record=store.store(
                artifact_type="summary",
                name=f"summary/{ledger.run_id}.json",
                data=b"{}",
                version=1,
            ),
        )

    for _ in range(2):
        assert (
            run_summarization_command(
                session_path=session_dir,
                manual_notes=None,
                language=None,
                settings=AppSettings.from_env(env={}, home=tmp_path),
                runtime_profile_selector_fn=_runtime_selection,
                runtime_profile_store_fn=_store,
                runtime_summarization_binder_fn=lambda **_kwargs: SimpleNamespace(),
                run_summarization_stage_fn=_stage,
            )
            == 0
        )

    assert len(run_ids) == 2
    assert len(set(run_ids)) == 2
    assert all(run_id.startswith("summarize-") for run_id in run_ids)


def test_summarize_workflow_releases_terminal_runtime_after_successful_run(
    tmp_path: Path,
) -> None:
    session_dir, _, _, _ = _build_summarize_session_bundle(tmp_path)
    releases: list[dict[str, object]] = []

    def _release(**kwargs: object) -> None:
        releases.append(kwargs)

    def _stage(**kwargs: object) -> SimpleNamespace:
        store = cast(ArtifactStore, kwargs["store"])
        return SimpleNamespace(
            summary_record=store.store(
                artifact_type="summary",
                name="summary/final.json",
                data=b"{}",
                version=1,
            ),
        )

    assert (
        run_summarization_command(
            session_path=session_dir,
            manual_notes=None,
            language=None,
            settings=AppSettings.from_env(env={}, home=tmp_path),
            runtime_profile_selector_fn=_runtime_selection,
            runtime_profile_store_fn=lambda **_kwargs: None,
            runtime_summarization_binder_fn=lambda **_kwargs: SimpleNamespace(),
            run_summarization_stage_fn=_stage,
            terminal_llm_release_fn=_release,
        )
        == 0
    )

    assert len(releases) == 1
    assert releases[0]["stage"] == "summarization"
    assert releases[0]["boundary"] == "summarize"
    assert releases[0]["attempt"] == 1


def test_summarize_workflow_releases_terminal_runtime_after_stage_failure(
    tmp_path: Path,
) -> None:
    session_dir, _, _, _ = _build_summarize_session_bundle(tmp_path)
    releases: list[dict[str, object]] = []

    def _release(**kwargs: object) -> None:
        releases.append(kwargs)

    with pytest.raises(SummarizationError, match="provider unavailable"):
        run_summarization_command(
            session_path=session_dir,
            manual_notes=None,
            language=None,
            settings=AppSettings.from_env(env={}, home=tmp_path),
            runtime_profile_selector_fn=_runtime_selection,
            runtime_profile_store_fn=lambda **_kwargs: None,
            runtime_summarization_binder_fn=lambda **_kwargs: SimpleNamespace(),
            run_summarization_stage_fn=lambda **_kwargs: (_ for _ in ()).throw(
                SummarizationError("provider unavailable")
            ),
            terminal_llm_release_fn=_release,
        )

    assert releases[0]["stage"] == "summarization"
    assert releases[0]["boundary"] == "summarize"
    assert releases[0]["attempt"] == 1
