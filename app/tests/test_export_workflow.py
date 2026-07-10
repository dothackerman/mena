import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from mena.artifacts import ArtifactRecord, FileSystemArtifactStore, session_artifact_manifest_path
from mena.export import SUMMARY_MARKDOWN_EXPORT_ARTIFACT_TYPE
from mena.export_workflow import ExportCommandInput, ExportFormat, run_export_workflow
from mena.interaction import ScriptedInteraction, ScriptedResponse
from mena.merge_timing import MERGED_TRANSCRIPT_ARTIFACT_TYPE
from mena.ollama_summarization import (
    OLLAMA_SUMMARIZATION_NUM_CTX,
    OLLAMA_SUMMARIZATION_SEED,
)
from mena.processing import ArtifactRef
from mena.session_integrity import SessionIntegrityError
from mena.sessions import SESSION_RECORD_FILENAME, SESSION_RECORD_VERSION
from mena.settings import AppSettings
from mena.summarization import (
    REQUIRED_NOTE_PRODUCTS,
    SUMMARY_ARTIFACT_VERSION,
    FakeSummarizationProvider,
    ProductRequestRecord,
    SummarizationPolicy,
    SummaryArtifact,
    SummaryResult,
    SummaryTimelineEntry,
    persist_summary_artifact,
)

SESSION_ID = "00000000-0000-0000-0000-000000000001"


def test_export_workflow_exports_latest_session_with_markdown_confirmation(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    older_dir, older_store, older_merged = _build_export_session(
        settings.paths.sessions_dir / "session-older"
    )
    _persist_summary(
        store=older_store,
        merged_transcript_record=older_merged,
        summary_created_at="2026-05-20T10:00:00Z",
    )
    newer_dir, newer_store, newer_merged = _build_export_session(
        settings.paths.sessions_dir / "session-newer"
    )
    _persist_summary(
        store=newer_store,
        merged_transcript_record=newer_merged,
        summary_created_at="2026-06-15T10:00:00Z",
    )
    interaction = ScriptedInteraction(
        (ScriptedResponse(kind="confirm", topic="export.format.markdown", value=True),)
    )

    result = run_export_workflow(
        ExportCommandInput(session_path=None),
        settings=settings,
        interaction=interaction,
    )

    assert result.status == "success"
    assert result.export_path is not None
    assert str(result.export_path).startswith(str(newer_dir))
    assert not str(result.export_path).startswith(str(older_dir))
    assert [request.topic for request in interaction.requests] == ["export.format.markdown"]
    assert [notice.topic for notice in interaction.notices] == [
        "export.session_selected.latest",
        "export.completed",
    ]
    assert interaction.notices[0].context["session_path"] == newer_dir


def test_export_workflow_declined_confirmation_cancels_without_export(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    session_dir, store, merged_record = _build_export_session(
        settings.paths.sessions_dir / "session"
    )
    _persist_summary(store=store, merged_transcript_record=merged_record)
    interaction = ScriptedInteraction(
        (ScriptedResponse(kind="confirm", topic="export.format.markdown", value=False),)
    )

    result = run_export_workflow(
        ExportCommandInput(session_path=session_dir),
        settings=settings,
        interaction=interaction,
    )

    assert result.status == "cancelled"
    assert result.export_path is None
    assert store.list(artifact_type=SUMMARY_MARKDOWN_EXPORT_ARTIFACT_TYPE) == ()


def test_export_workflow_format_flag_skips_confirmation(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    session_dir, store, merged_record = _build_export_session(
        settings.paths.sessions_dir / "session"
    )
    _persist_summary(store=store, merged_transcript_record=merged_record)
    interaction = ScriptedInteraction(())

    result = run_export_workflow(
        ExportCommandInput(session_path=session_dir, export_format="markdown"),
        settings=settings,
        interaction=interaction,
    )

    assert result.status == "success"
    assert interaction.requests == []
    assert [notice.topic for notice in interaction.notices] == ["export.completed"]


def test_export_workflow_rejects_unsupported_format_without_cli_parser(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    session_dir, store, merged_record = _build_export_session(
        settings.paths.sessions_dir / "session"
    )
    _persist_summary(store=store, merged_transcript_record=merged_record)

    with pytest.raises(ValueError, match="Unsupported export format: pdf"):
        run_export_workflow(
            ExportCommandInput(
                session_path=session_dir,
                export_format=cast(ExportFormat, "pdf"),
            ),
            settings=settings,
            interaction=ScriptedInteraction(()),
        )

    assert store.list(artifact_type=SUMMARY_MARKDOWN_EXPORT_ARTIFACT_TYPE) == ()


def test_export_workflow_payload_timestamp_overrides_stale_metadata(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    stale_dir, stale_store, stale_merged = _build_export_session(
        settings.paths.sessions_dir / "session-stale-metadata"
    )
    stale_summary = _persist_summary(
        store=stale_store,
        merged_transcript_record=stale_merged,
        summary_created_at="2026-05-20T10:00:00Z",
    )
    _update_artifact_manifest_record(
        stale_dir,
        artifact_id=stale_summary.id,
        metadata_updates={"summary_created_at": "2026-07-03T09:00:00Z"},
    )
    current_dir, current_store, current_merged = _build_export_session(
        settings.paths.sessions_dir / "session-current"
    )
    _persist_summary(
        store=current_store,
        merged_transcript_record=current_merged,
        summary_created_at="2026-06-15T10:00:00Z",
    )

    result = run_export_workflow(
        ExportCommandInput(session_path=None, export_format="markdown"),
        settings=settings,
        interaction=ScriptedInteraction(()),
    )

    assert result.status == "success"
    assert result.export_path is not None
    assert str(result.export_path).startswith(str(current_dir))


def test_export_workflow_skips_malformed_unrelated_session_during_default_discovery(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    malformed_dir, malformed_store, malformed_merged = _build_export_session(
        settings.paths.sessions_dir / "session-malformed"
    )
    _persist_summary(
        store=malformed_store,
        merged_transcript_record=malformed_merged,
        summary_created_at="2026-07-03T09:00:00Z",
    )
    malformed_record_path = malformed_dir / SESSION_RECORD_FILENAME
    malformed_record = json.loads(malformed_record_path.read_text(encoding="utf-8"))
    malformed_record.pop("id")
    malformed_record_path.write_text(json.dumps(malformed_record), encoding="utf-8")
    valid_dir, valid_store, valid_merged = _build_export_session(
        settings.paths.sessions_dir / "session-valid"
    )
    _persist_summary(
        store=valid_store,
        merged_transcript_record=valid_merged,
        summary_created_at="2026-07-03T08:00:00Z",
    )

    result = run_export_workflow(
        ExportCommandInput(session_path=None, export_format="markdown"),
        settings=settings,
        interaction=ScriptedInteraction(()),
    )

    assert result.status == "success"
    assert result.export_path is not None
    assert str(result.export_path).startswith(str(valid_dir))


def test_export_workflow_default_discovery_does_not_create_unsummarized_artifacts_dir(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    unsummarized_dir = _create_session_record(settings.paths.sessions_dir / "session-unsummarized")
    valid_dir, valid_store, valid_merged = _build_export_session(
        settings.paths.sessions_dir / "session-valid"
    )
    _persist_summary(
        store=valid_store,
        merged_transcript_record=valid_merged,
        summary_created_at="2026-07-03T09:00:00Z",
    )

    assert not (unsummarized_dir / "artifacts").exists()

    result = run_export_workflow(
        ExportCommandInput(session_path=None, export_format="markdown"),
        settings=settings,
        interaction=ScriptedInteraction(()),
    )

    assert result.status == "success"
    assert result.export_path is not None
    assert str(result.export_path).startswith(str(valid_dir))
    assert not (unsummarized_dir / "artifacts").exists()


def test_export_workflow_reports_when_no_summarized_sessions(tmp_path: Path) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)

    with pytest.raises(ValueError, match="No summarized sessions were found"):
        run_export_workflow(
            ExportCommandInput(session_path=None, export_format="markdown"),
            settings=settings,
            interaction=ScriptedInteraction(()),
        )


def test_export_workflow_skips_malformed_manifest_during_default_discovery(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    malformed_dir, malformed_store, malformed_merged = _build_export_session(
        settings.paths.sessions_dir / "session-malformed"
    )
    _persist_summary(
        store=malformed_store,
        merged_transcript_record=malformed_merged,
        summary_created_at="2026-07-03T09:00:00Z",
    )
    session_artifact_manifest_path(malformed_dir).write_text("{", encoding="utf-8")
    valid_dir, valid_store, valid_merged = _build_export_session(
        settings.paths.sessions_dir / "session-valid"
    )
    _persist_summary(
        store=valid_store,
        merged_transcript_record=valid_merged,
        summary_created_at="2026-07-03T08:00:00Z",
    )

    result = run_export_workflow(
        ExportCommandInput(session_path=None, export_format="markdown"),
        settings=settings,
        interaction=ScriptedInteraction(()),
    )

    assert result.status == "success"
    assert result.export_path is not None
    assert str(result.export_path).startswith(str(valid_dir))


def test_export_workflow_reports_selected_session_with_malformed_unrelated_record(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    _, older_store, older_merged = _build_export_session(
        settings.paths.sessions_dir / "session-older"
    )
    _persist_summary(
        store=older_store,
        merged_transcript_record=older_merged,
        summary_created_at="2026-07-03T08:00:00Z",
    )
    malformed_dir, malformed_store, malformed_merged = _build_export_session(
        settings.paths.sessions_dir / "session-newer-malformed"
    )
    _persist_summary(
        store=malformed_store,
        merged_transcript_record=malformed_merged,
        summary_created_at="2026-07-03T09:00:00Z",
    )
    _append_malformed_artifact_manifest_record(malformed_dir)
    interaction = ScriptedInteraction(())

    with pytest.raises(SessionIntegrityError, match="artifact_record_invalid"):
        run_export_workflow(
            ExportCommandInput(session_path=None, export_format="markdown"),
            settings=settings,
            interaction=interaction,
        )

    assert interaction.notices == []


def test_export_workflow_reports_selected_session_with_malformed_summary_record(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    _, older_store, older_merged = _build_export_session(
        settings.paths.sessions_dir / "session-older"
    )
    _persist_summary(
        store=older_store,
        merged_transcript_record=older_merged,
        summary_created_at="2026-07-03T08:00:00Z",
    )
    malformed_dir, malformed_store, malformed_merged = _build_export_session(
        settings.paths.sessions_dir / "session-newer-malformed"
    )
    malformed_summary = _persist_summary(
        store=malformed_store,
        merged_transcript_record=malformed_merged,
        summary_created_at="2026-07-03T09:00:00Z",
    )
    _update_artifact_manifest_record(
        malformed_dir,
        artifact_id=malformed_summary.id,
        updates={"sha256": "not-a-digest"},
    )

    with pytest.raises(SessionIntegrityError, match="artifact_record_invalid"):
        run_export_workflow(
            ExportCommandInput(session_path=None, export_format="markdown"),
            settings=settings,
            interaction=ScriptedInteraction(()),
        )


def test_export_workflow_reports_selected_session_with_malformed_summary_status(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    _, older_store, older_merged = _build_export_session(
        settings.paths.sessions_dir / "session-older"
    )
    _persist_summary(
        store=older_store,
        merged_transcript_record=older_merged,
        summary_created_at="2026-07-03T08:00:00Z",
    )
    malformed_dir, malformed_store, malformed_merged = _build_export_session(
        settings.paths.sessions_dir / "session-newer-malformed"
    )
    malformed_summary = _persist_summary(
        store=malformed_store,
        merged_transcript_record=malformed_merged,
        summary_created_at="2026-07-03T09:00:00Z",
    )
    _update_artifact_manifest_record(
        malformed_dir,
        artifact_id=malformed_summary.id,
        updates={"status": "sealedd"},
    )

    with pytest.raises(SessionIntegrityError, match="artifact_record_invalid"):
        run_export_workflow(
            ExportCommandInput(session_path=None, export_format="markdown"),
            settings=settings,
            interaction=ScriptedInteraction(()),
        )


def test_export_workflow_skips_corrupt_non_selected_summary(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    corrupt_dir, corrupt_store, corrupt_merged = _build_export_session(
        settings.paths.sessions_dir / "session-older-corrupt"
    )
    corrupt_summary = _persist_summary(
        store=corrupt_store,
        merged_transcript_record=corrupt_merged,
        summary_created_at="2026-07-03T08:00:00Z",
    )
    (corrupt_dir / corrupt_summary.path).write_text("{", encoding="utf-8")
    valid_dir, valid_store, valid_merged = _build_export_session(
        settings.paths.sessions_dir / "session-newer-valid"
    )
    _persist_summary(
        store=valid_store,
        merged_transcript_record=valid_merged,
        summary_created_at="2026-07-03T09:00:00Z",
    )

    result = run_export_workflow(
        ExportCommandInput(session_path=None, export_format="markdown"),
        settings=settings,
        interaction=ScriptedInteraction(()),
    )

    assert result.status == "success"
    assert result.export_path is not None
    assert str(result.export_path).startswith(str(valid_dir))


def test_export_workflow_skips_corrupt_summary_without_ranking_metadata(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    older_dir, older_store, older_merged = _build_export_session(
        settings.paths.sessions_dir / "session-older"
    )
    _persist_summary(
        store=older_store,
        merged_transcript_record=older_merged,
        summary_created_at="2026-07-03T08:00:00Z",
    )
    corrupt_dir, corrupt_store, corrupt_merged = _build_export_session(
        settings.paths.sessions_dir / "session-legacy-corrupt"
    )
    corrupt_summary = _persist_summary(
        store=corrupt_store,
        merged_transcript_record=corrupt_merged,
        summary_created_at="2026-07-03T09:00:00Z",
    )
    _update_artifact_manifest_record(
        corrupt_dir,
        artifact_id=corrupt_summary.id,
        metadata_updates={"summary_created_at": None},
    )
    (corrupt_dir / corrupt_summary.path).write_text("{", encoding="utf-8")

    result = run_export_workflow(
        ExportCommandInput(session_path=None, export_format="markdown"),
        settings=settings,
        interaction=ScriptedInteraction(()),
    )

    assert result.status == "success"
    assert result.export_path is not None
    assert str(result.export_path).startswith(str(older_dir))


def test_export_workflow_reports_corrupt_newest_summary_during_default_discovery(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    _, older_store, older_merged = _build_export_session(
        settings.paths.sessions_dir / "session-older"
    )
    _persist_summary(
        store=older_store,
        merged_transcript_record=older_merged,
        summary_created_at="2026-07-03T08:00:00Z",
    )
    corrupt_dir, corrupt_store, corrupt_merged = _build_export_session(
        settings.paths.sessions_dir / "session-newer-corrupt"
    )
    corrupt_summary = _persist_summary(
        store=corrupt_store,
        merged_transcript_record=corrupt_merged,
        summary_created_at="2026-07-03T09:00:00Z",
    )
    (corrupt_dir / corrupt_summary.path).write_text("{", encoding="utf-8")

    with pytest.raises(SessionIntegrityError):
        run_export_workflow(
            ExportCommandInput(session_path=None, export_format="markdown"),
            settings=settings,
            interaction=ScriptedInteraction(()),
        )


def test_export_workflow_selects_requested_summary_artifact_session(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    older_dir, older_store, older_merged = _build_export_session(
        settings.paths.sessions_dir / "session-older"
    )
    older_summary = _persist_summary(
        store=older_store,
        merged_transcript_record=older_merged,
        summary_created_at="2026-05-20T10:00:00Z",
        summary_name="summary/older-final.json",
    )
    _, newer_store, newer_merged = _build_export_session(
        settings.paths.sessions_dir / "session-newer"
    )
    _persist_summary(
        store=newer_store,
        merged_transcript_record=newer_merged,
        summary_created_at="2026-06-15T10:00:00Z",
        summary_name="summary/newer-final.json",
    )
    interaction = ScriptedInteraction(())

    result = run_export_workflow(
        ExportCommandInput(
            session_path=None,
            summary_artifact_id=older_summary.id,
            export_format="markdown",
        ),
        settings=settings,
        interaction=interaction,
    )

    assert result.status == "success"
    assert result.export_path is not None
    assert str(result.export_path).startswith(str(older_dir))
    assert [notice.topic for notice in interaction.notices] == [
        "export.session_selected.summary_artifact",
        "export.completed",
    ]


def test_export_workflow_requested_artifact_skips_malformed_unrelated_session(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    malformed_dir, malformed_store, malformed_merged = _build_export_session(
        settings.paths.sessions_dir / "session-malformed"
    )
    _persist_summary(
        store=malformed_store,
        merged_transcript_record=malformed_merged,
        summary_name="summary/unrelated.json",
    )
    malformed_record_path = malformed_dir / SESSION_RECORD_FILENAME
    malformed_record = json.loads(malformed_record_path.read_text(encoding="utf-8"))
    malformed_record.pop("id")
    malformed_record_path.write_text(json.dumps(malformed_record), encoding="utf-8")
    target_dir, target_store, target_merged = _build_export_session(
        settings.paths.sessions_dir / "session-target"
    )
    requested_summary = _persist_summary(
        store=target_store,
        merged_transcript_record=target_merged,
        summary_name="summary/requested.json",
    )

    result = run_export_workflow(
        ExportCommandInput(
            session_path=None,
            summary_artifact_id=requested_summary.id,
            export_format="markdown",
        ),
        settings=settings,
        interaction=ScriptedInteraction(()),
    )

    assert result.status == "success"
    assert result.export_path is not None
    assert str(result.export_path).startswith(str(target_dir))


def test_export_workflow_reports_requested_artifact_with_malformed_session_record(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    target_dir, target_store, target_merged = _build_export_session(
        settings.paths.sessions_dir / "session-target"
    )
    requested_summary = _persist_summary(
        store=target_store,
        merged_transcript_record=target_merged,
        summary_name="summary/requested.json",
    )
    target_record_path = target_dir / SESSION_RECORD_FILENAME
    target_record = json.loads(target_record_path.read_text(encoding="utf-8"))
    target_record.pop("id")
    target_record_path.write_text(json.dumps(target_record), encoding="utf-8")
    _, valid_store, valid_merged = _build_export_session(
        settings.paths.sessions_dir / "session-valid"
    )
    _persist_summary(
        store=valid_store,
        merged_transcript_record=valid_merged,
        summary_name="summary/valid.json",
    )

    with pytest.raises(SessionIntegrityError):
        run_export_workflow(
            ExportCommandInput(
                session_path=None,
                summary_artifact_id=requested_summary.id,
                export_format="markdown",
            ),
            settings=settings,
            interaction=ScriptedInteraction(()),
        )


def test_export_workflow_reports_requested_summary_artifact_with_malformed_status(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    malformed_dir, malformed_store, malformed_merged = _build_export_session(
        settings.paths.sessions_dir / "session-malformed"
    )
    malformed_summary = _persist_summary(
        store=malformed_store,
        merged_transcript_record=malformed_merged,
        summary_name="summary/requested.json",
    )
    _update_artifact_manifest_record(
        malformed_dir,
        artifact_id=malformed_summary.id,
        updates={"status": "sealedd"},
    )
    _, valid_store, valid_merged = _build_export_session(
        settings.paths.sessions_dir / "session-valid"
    )
    _persist_summary(
        store=valid_store,
        merged_transcript_record=valid_merged,
        summary_name="summary/valid.json",
    )

    with pytest.raises(SessionIntegrityError, match="artifact_record_invalid"):
        run_export_workflow(
            ExportCommandInput(
                session_path=None,
                summary_artifact_id=malformed_summary.id,
                export_format="markdown",
            ),
            settings=settings,
            interaction=ScriptedInteraction(()),
        )


@pytest.mark.parametrize("artifact_type", [None, ["summary"]])
def test_export_workflow_reports_requested_summary_artifact_with_malformed_type(
    tmp_path: Path,
    artifact_type: object,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    malformed_dir, malformed_store, malformed_merged = _build_export_session(
        settings.paths.sessions_dir / "session-malformed"
    )
    malformed_summary = _persist_summary(
        store=malformed_store,
        merged_transcript_record=malformed_merged,
        summary_name="summary/requested.json",
    )
    _update_artifact_manifest_record(
        malformed_dir,
        artifact_id=malformed_summary.id,
        updates={"artifact_type": artifact_type},
    )
    _, valid_store, valid_merged = _build_export_session(
        settings.paths.sessions_dir / "session-valid"
    )
    _persist_summary(
        store=valid_store,
        merged_transcript_record=valid_merged,
        summary_name="summary/valid.json",
    )

    with pytest.raises(SessionIntegrityError, match="artifact_record_invalid"):
        run_export_workflow(
            ExportCommandInput(
                session_path=None,
                summary_artifact_id=malformed_summary.id,
                export_format="markdown",
            ),
            settings=settings,
            interaction=ScriptedInteraction(()),
        )


def test_export_workflow_reports_requested_summary_artifact_with_session_id_mismatch(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    malformed_dir, malformed_store, malformed_merged = _build_export_session(
        settings.paths.sessions_dir / "session-malformed"
    )
    malformed_summary = _persist_summary(
        store=malformed_store,
        merged_transcript_record=malformed_merged,
        summary_name="summary/requested.json",
    )
    mismatched_session_id = "00000000-0000-0000-0000-000000000099"
    mismatched_summary_id = f"{mismatched_session_id}:{malformed_summary.path}"
    _update_artifact_manifest_record(
        malformed_dir,
        artifact_id=malformed_summary.id,
        updates={
            "id": mismatched_summary_id,
            "session_id": mismatched_session_id,
        },
    )
    _, valid_store, valid_merged = _build_export_session(
        settings.paths.sessions_dir / "session-valid"
    )
    _persist_summary(
        store=valid_store,
        merged_transcript_record=valid_merged,
        summary_name="summary/valid.json",
    )

    with pytest.raises(SessionIntegrityError, match="artifact_record_session_mismatch"):
        run_export_workflow(
            ExportCommandInput(
                session_path=None,
                summary_artifact_id=mismatched_summary_id,
                export_format="markdown",
            ),
            settings=settings,
            interaction=ScriptedInteraction(()),
        )


def test_export_workflow_reports_requested_summary_artifact_with_non_string_status(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    malformed_dir, malformed_store, malformed_merged = _build_export_session(
        settings.paths.sessions_dir / "session-malformed"
    )
    malformed_summary = _persist_summary(
        store=malformed_store,
        merged_transcript_record=malformed_merged,
        summary_name="summary/requested.json",
    )
    _update_artifact_manifest_record(
        malformed_dir,
        artifact_id=malformed_summary.id,
        updates={"status": ["sealed"]},
    )
    _, valid_store, valid_merged = _build_export_session(
        settings.paths.sessions_dir / "session-valid"
    )
    _persist_summary(
        store=valid_store,
        merged_transcript_record=valid_merged,
        summary_name="summary/valid.json",
    )

    with pytest.raises(SessionIntegrityError, match="artifact_record_invalid"):
        run_export_workflow(
            ExportCommandInput(
                session_path=None,
                summary_artifact_id=malformed_summary.id,
                export_format="markdown",
            ),
            settings=settings,
            interaction=ScriptedInteraction(()),
        )


def test_export_workflow_reports_corrupt_requested_summary_artifact(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    corrupt_dir, corrupt_store, corrupt_merged = _build_export_session(
        settings.paths.sessions_dir / "session-corrupt"
    )
    corrupt_summary = _persist_summary(
        store=corrupt_store,
        merged_transcript_record=corrupt_merged,
        summary_name="summary/corrupt.json",
    )
    (corrupt_dir / corrupt_summary.path).write_text("{", encoding="utf-8")
    _, valid_store, valid_merged = _build_export_session(
        settings.paths.sessions_dir / "session-valid"
    )
    _persist_summary(
        store=valid_store,
        merged_transcript_record=valid_merged,
        summary_name="summary/valid.json",
    )

    with pytest.raises(SessionIntegrityError):
        run_export_workflow(
            ExportCommandInput(
                session_path=None,
                summary_artifact_id=corrupt_summary.id,
                export_format="markdown",
            ),
            settings=settings,
            interaction=ScriptedInteraction(()),
        )


def test_export_workflow_ranks_invalid_summary_payload_timestamp_as_oldest(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    stale_dir, stale_store, stale_merged = _build_export_session(
        settings.paths.sessions_dir / "session-stale-invalid-timestamp"
    )
    stale_summary = _persist_summary(
        store=stale_store,
        merged_transcript_record=stale_merged,
        summary_created_at="2026-05-20T10:00:00Z",
    )
    stale_payload_path = stale_dir / stale_summary.path
    stale_payload = json.loads(stale_payload_path.read_text(encoding="utf-8"))
    stale_payload["created_at"] = "not-a-timestamp"
    stale_payload_path.write_text(json.dumps(stale_payload), encoding="utf-8")
    current_dir, current_store, current_merged = _build_export_session(
        settings.paths.sessions_dir / "session-current"
    )
    _persist_summary(
        store=current_store,
        merged_transcript_record=current_merged,
        summary_created_at="2026-06-15T10:00:00Z",
    )

    result = run_export_workflow(
        ExportCommandInput(session_path=None, export_format="markdown"),
        settings=settings,
        interaction=ScriptedInteraction(()),
    )

    assert result.status == "success"
    assert result.export_path is not None
    assert str(result.export_path).startswith(str(current_dir))


def test_export_workflow_skips_incompatible_summary_payload_during_default_discovery(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    valid_dir, valid_store, valid_merged = _build_export_session(
        settings.paths.sessions_dir / "session-older-valid"
    )
    _persist_summary(
        store=valid_store,
        merged_transcript_record=valid_merged,
        summary_created_at="2026-07-03T08:00:00Z",
    )
    incompatible_dir, incompatible_store, incompatible_merged = _build_export_session(
        settings.paths.sessions_dir / "session-newer-incompatible"
    )
    incompatible_summary = _persist_summary(
        store=incompatible_store,
        merged_transcript_record=incompatible_merged,
        summary_created_at="2026-07-03T09:00:00Z",
    )
    # A sealed summary payload that stays readable and version-matched but no
    # longer satisfies the strict contract (here missing request_records, as a
    # pre-note-package payload would). Keep the sealed record consistent with the
    # rewritten bytes so this is a contract incompatibility, not sha corruption.
    incompatible_payload_path = incompatible_dir / incompatible_summary.path
    incompatible_payload = json.loads(incompatible_payload_path.read_text(encoding="utf-8"))
    del incompatible_payload["request_records"]
    incompatible_bytes = (json.dumps(incompatible_payload, sort_keys=True) + "\n").encode("utf-8")
    incompatible_payload_path.write_bytes(incompatible_bytes)
    _update_artifact_manifest_record(
        incompatible_dir,
        artifact_id=incompatible_summary.id,
        updates={
            "sha256": hashlib.sha256(incompatible_bytes).hexdigest(),
            "byte_count": len(incompatible_bytes),
        },
    )

    result = run_export_workflow(
        ExportCommandInput(session_path=None, export_format="markdown"),
        settings=settings,
        interaction=ScriptedInteraction(()),
    )

    assert result.status == "success"
    assert result.export_path is not None
    assert str(result.export_path).startswith(str(valid_dir))


def test_export_workflow_reports_session_id_mismatch_summary_during_default_discovery(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    mismatch_dir, mismatch_store, mismatch_merged = _build_export_session(
        settings.paths.sessions_dir / "session-newer-mismatch"
    )
    mismatch_summary = _persist_summary(
        store=mismatch_store,
        merged_transcript_record=mismatch_merged,
        summary_created_at="2026-07-03T09:00:00Z",
    )
    mismatched_session_id = "00000000-0000-0000-0000-000000000099"
    _update_artifact_manifest_record(
        mismatch_dir,
        artifact_id=mismatch_summary.id,
        updates={
            "id": f"{mismatched_session_id}:{mismatch_summary.path}",
            "session_id": mismatched_session_id,
        },
    )
    _, valid_store, valid_merged = _build_export_session(
        settings.paths.sessions_dir / "session-older-valid"
    )
    _persist_summary(
        store=valid_store,
        merged_transcript_record=valid_merged,
        summary_created_at="2026-07-03T08:00:00Z",
    )

    with pytest.raises(SessionIntegrityError, match="artifact_record_session_mismatch"):
        run_export_workflow(
            ExportCommandInput(session_path=None, export_format="markdown"),
            settings=settings,
            interaction=ScriptedInteraction(()),
        )


def test_export_workflow_reports_manifest_session_mismatch_during_default_discovery(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    mismatch_dir, mismatch_store, mismatch_merged = _build_export_session(
        settings.paths.sessions_dir / "session-newer-manifest-mismatch"
    )
    _persist_summary(
        store=mismatch_store,
        merged_transcript_record=mismatch_merged,
        summary_created_at="2026-07-03T09:00:00Z",
    )
    manifest_path = session_artifact_manifest_path(mismatch_dir)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["session_id"] = "00000000-0000-0000-0000-000000000099"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _, valid_store, valid_merged = _build_export_session(
        settings.paths.sessions_dir / "session-older-valid"
    )
    _persist_summary(
        store=valid_store,
        merged_transcript_record=valid_merged,
        summary_created_at="2026-07-03T08:00:00Z",
    )

    with pytest.raises(SessionIntegrityError, match="artifact_manifest_session_mismatch"):
        run_export_workflow(
            ExportCommandInput(session_path=None, export_format="markdown"),
            settings=settings,
            interaction=ScriptedInteraction(()),
        )


def test_export_workflow_reports_manifest_version_mismatch_during_default_discovery(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    mismatch_dir, mismatch_store, mismatch_merged = _build_export_session(
        settings.paths.sessions_dir / "session-newer-manifest-version"
    )
    _persist_summary(
        store=mismatch_store,
        merged_transcript_record=mismatch_merged,
        summary_created_at="2026-07-03T09:00:00Z",
    )
    manifest_path = session_artifact_manifest_path(mismatch_dir)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = manifest["version"] + 999
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _, valid_store, valid_merged = _build_export_session(
        settings.paths.sessions_dir / "session-older-valid"
    )
    _persist_summary(
        store=valid_store,
        merged_transcript_record=valid_merged,
        summary_created_at="2026-07-03T08:00:00Z",
    )

    with pytest.raises(SessionIntegrityError, match="artifact_manifest_version_unsupported"):
        run_export_workflow(
            ExportCommandInput(session_path=None, export_format="markdown"),
            settings=settings,
            interaction=ScriptedInteraction(()),
        )


def test_export_workflow_skips_legacy_record_version_during_default_discovery(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    legacy_dir, legacy_store, legacy_merged = _build_export_session(
        settings.paths.sessions_dir / "session-newer-legacy-record"
    )
    legacy_summary = _persist_summary(
        store=legacy_store,
        merged_transcript_record=legacy_merged,
        summary_created_at="2026-07-03T09:00:00Z",
    )
    # A sealed summary record whose artifact-record version is not the current
    # SUMMARY_ARTIFACT_VERSION, while its payload stays a readable v2. Export
    # selection filters such records out, so discovery must rank it oldest.
    _update_artifact_manifest_record(
        legacy_dir,
        artifact_id=legacy_summary.id,
        updates={"version": 1},
    )
    valid_dir, valid_store, valid_merged = _build_export_session(
        settings.paths.sessions_dir / "session-older-valid"
    )
    _persist_summary(
        store=valid_store,
        merged_transcript_record=valid_merged,
        summary_created_at="2026-07-03T08:00:00Z",
    )

    result = run_export_workflow(
        ExportCommandInput(session_path=None, export_format="markdown"),
        settings=settings,
        interaction=ScriptedInteraction(()),
    )

    assert result.status == "success"
    assert result.export_path is not None
    assert str(result.export_path).startswith(str(valid_dir))


def test_export_workflow_skips_incompatible_payload_version_during_default_discovery(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    incompatible_dir, incompatible_store, incompatible_merged = _build_export_session(
        settings.paths.sessions_dir / "session-newer-bad-payload-version"
    )
    incompatible_summary = _persist_summary(
        store=incompatible_store,
        merged_transcript_record=incompatible_merged,
        summary_created_at="2026-07-03T09:00:00Z",
    )
    # A sealed summary whose payload version is not SUMMARY_SCHEMA_VERSION, kept
    # hash-consistent so this is a version incompatibility, not corruption.
    incompatible_payload_path = incompatible_dir / incompatible_summary.path
    incompatible_payload = json.loads(incompatible_payload_path.read_text(encoding="utf-8"))
    incompatible_payload["version"] = incompatible_payload["version"] + 999
    incompatible_bytes = (json.dumps(incompatible_payload, sort_keys=True) + "\n").encode("utf-8")
    incompatible_payload_path.write_bytes(incompatible_bytes)
    _update_artifact_manifest_record(
        incompatible_dir,
        artifact_id=incompatible_summary.id,
        updates={
            "sha256": hashlib.sha256(incompatible_bytes).hexdigest(),
            "byte_count": len(incompatible_bytes),
        },
    )
    valid_dir, valid_store, valid_merged = _build_export_session(
        settings.paths.sessions_dir / "session-older-valid"
    )
    _persist_summary(
        store=valid_store,
        merged_transcript_record=valid_merged,
        summary_created_at="2026-07-03T08:00:00Z",
    )

    result = run_export_workflow(
        ExportCommandInput(session_path=None, export_format="markdown"),
        settings=settings,
        interaction=ScriptedInteraction(()),
    )

    assert result.status == "success"
    assert result.export_path is not None
    assert str(result.export_path).startswith(str(valid_dir))


def test_export_workflow_skips_non_sealed_summary_in_untrusted_manifest(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    untrusted_dir, untrusted_store, untrusted_merged = _build_export_session(
        settings.paths.sessions_dir / "session-newer-untrusted-nonsealed"
    )
    untrusted_summary = _persist_summary(
        store=untrusted_store,
        merged_transcript_record=untrusted_merged,
        summary_created_at="2026-07-03T09:00:00Z",
    )
    # Untrusted manifest (session-id mismatch) whose only summary record is
    # non-sealed. It is not an export candidate, so it must not drive fail-closed
    # selection over a genuinely exportable older session.
    _update_artifact_manifest_record(
        untrusted_dir,
        artifact_id=untrusted_summary.id,
        updates={"status": "staged"},
    )
    manifest_path = session_artifact_manifest_path(untrusted_dir)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["session_id"] = "00000000-0000-0000-0000-000000000099"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    valid_dir, valid_store, valid_merged = _build_export_session(
        settings.paths.sessions_dir / "session-older-valid"
    )
    _persist_summary(
        store=valid_store,
        merged_transcript_record=valid_merged,
        summary_created_at="2026-07-03T08:00:00Z",
    )

    result = run_export_workflow(
        ExportCommandInput(session_path=None, export_format="markdown"),
        settings=settings,
        interaction=ScriptedInteraction(()),
    )

    assert result.status == "success"
    assert result.export_path is not None
    assert str(result.export_path).startswith(str(valid_dir))


def test_export_workflow_skips_non_object_summary_payload_during_default_discovery(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    incompatible_dir, incompatible_store, incompatible_merged = _build_export_session(
        settings.paths.sessions_dir / "session-newer-non-object-payload"
    )
    incompatible_summary = _persist_summary(
        store=incompatible_store,
        merged_transcript_record=incompatible_merged,
        summary_created_at="2026-07-03T09:00:00Z",
    )
    # A sealed summary whose payload is valid JSON but not an object. Export
    # selection skips it, so discovery must rank it oldest rather than trusting
    # the manifest recency metadata. Keep the record hash-consistent.
    incompatible_payload_path = incompatible_dir / incompatible_summary.path
    incompatible_bytes = b"[]\n"
    incompatible_payload_path.write_bytes(incompatible_bytes)
    _update_artifact_manifest_record(
        incompatible_dir,
        artifact_id=incompatible_summary.id,
        updates={
            "sha256": hashlib.sha256(incompatible_bytes).hexdigest(),
            "byte_count": len(incompatible_bytes),
        },
    )
    valid_dir, valid_store, valid_merged = _build_export_session(
        settings.paths.sessions_dir / "session-older-valid"
    )
    _persist_summary(
        store=valid_store,
        merged_transcript_record=valid_merged,
        summary_created_at="2026-07-03T08:00:00Z",
    )

    result = run_export_workflow(
        ExportCommandInput(session_path=None, export_format="markdown"),
        settings=settings,
        interaction=ScriptedInteraction(()),
    )

    assert result.status == "success"
    assert result.export_path is not None
    assert str(result.export_path).startswith(str(valid_dir))


def test_export_workflow_reports_requested_summary_session_with_unreadable_manifest(
    tmp_path: Path,
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)
    target_dir, target_store, target_merged = _build_export_session(
        settings.paths.sessions_dir / "session-target"
    )
    requested_summary = _persist_summary(
        store=target_store,
        merged_transcript_record=target_merged,
        summary_name="summary/requested.json",
    )
    manifest_path = session_artifact_manifest_path(target_dir)
    manifest_path.unlink()
    manifest_path.mkdir()
    _, valid_store, valid_merged = _build_export_session(
        settings.paths.sessions_dir / "session-valid"
    )
    _persist_summary(
        store=valid_store,
        merged_transcript_record=valid_merged,
        summary_name="summary/valid.json",
    )

    with pytest.raises(SessionIntegrityError):
        run_export_workflow(
            ExportCommandInput(
                session_path=None,
                summary_artifact_id=requested_summary.id,
                export_format="markdown",
            ),
            settings=settings,
            interaction=ScriptedInteraction(()),
        )


def _build_export_session(
    session_dir: Path,
) -> tuple[Path, FileSystemArtifactStore, ArtifactRecord]:
    session_dir = _create_session_record(session_dir)
    store = FileSystemArtifactStore(session_id=SESSION_ID, session_dir=session_dir)
    merged_record = store.store(
        artifact_type=MERGED_TRANSCRIPT_ARTIFACT_TYPE,
        name="merged/merged.json",
        data=b'{"version":1}',
        version=1,
        status="sealed",
        metadata={"contract": "merged-transcript.v1"},
    )
    return session_dir, store, merged_record


def _create_session_record(session_dir: Path) -> Path:
    session_dir.mkdir(parents=True)
    (session_dir / SESSION_RECORD_FILENAME).write_text(
        json.dumps(
            {
                "version": SESSION_RECORD_VERSION,
                "id": SESSION_ID,
                "created_at": "2026-05-19T15:00:00Z",
                "name": None,
                "status": "created",
                "profile": "online_dual_stream",
                "language": "fr-CH",
                "recording": {
                    "started_at": None,
                    "stopped_at": None,
                    "stop_reason": None,
                    "finalization": {
                        "status": "not_started",
                        "completed_at": None,
                        "error": None,
                    },
                },
                "capture_plan": None,
                "degradations": [],
                "failures": [],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return session_dir


def _summary_result() -> SummaryResult:
    return SummaryResult(
        timeline=(
            SummaryTimelineEntry(
                text="Agenda covered.",
                source_segment_ids=("merged_segment_000001",),
                start_seconds=0.0,
            ),
        ),
        action_items=(),
        general_summary="The team aligned on the plan.",
        request_records=_request_records_for_products(REQUIRED_NOTE_PRODUCTS),
    )


def _persist_summary(
    *,
    store: FileSystemArtifactStore,
    merged_transcript_record: ArtifactRecord,
    summary_created_at: str = "2026-05-20T10:00:00Z",
    summary_name: str = "summary/final.json",
) -> ArtifactRecord:
    summary_result = _summary_result()
    return persist_summary_artifact(
        store=store,
        artifact=SummaryArtifact(
            version=SUMMARY_ARTIFACT_VERSION,
            session_id=SESSION_ID,
            merged_transcript_artifact=ArtifactRef.from_record(merged_transcript_record),
            manual_notes_artifact=None,
            policy=SummarizationPolicy(language="en").to_dict(),
            provider=FakeSummarizationProvider(result=summary_result).metadata,
            timeline=summary_result.timeline,
            action_items=summary_result.action_items,
            general_summary=summary_result.general_summary,
            created_at=summary_created_at,
            request_records=summary_result.request_records,
        ),
        name=summary_name,
        correction_overlay_record=None,
    )


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


def _update_artifact_manifest_record(
    session_dir: Path,
    *,
    artifact_id: str,
    updates: Mapping[str, object] | None = None,
    metadata_updates: Mapping[str, object | None] | None = None,
) -> None:
    manifest_path = session_artifact_manifest_path(session_dir)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest["artifacts"]:
        if item["id"] != artifact_id:
            continue
        if updates is not None:
            for key, value in updates.items():
                if value is None:
                    item.pop(key, None)
                else:
                    item[key] = value
        if metadata_updates is not None:
            metadata = item.setdefault("metadata", {})
            for key, value in metadata_updates.items():
                if value is None:
                    metadata.pop(key, None)
                else:
                    metadata[key] = value
        break
    else:
        raise AssertionError(f"Artifact record not found: {artifact_id}")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _append_malformed_artifact_manifest_record(session_dir: Path) -> None:
    manifest_path = session_artifact_manifest_path(session_dir)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"].append(
        {
            "artifact_type": "transcript",
            "path": "artifacts/transcript/malformed.json",
            "status": "sealed",
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
