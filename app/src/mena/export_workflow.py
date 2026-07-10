"""Export command workflow orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mena.artifacts import FileSystemArtifactStore
from mena.export import export_summary_markdown
from mena.exportable_summary import resolve_exportable_summary_session
from mena.interaction import ConfirmationRequest, InteractionPort, WorkflowNotice
from mena.session_integrity import load_verified_session_bundle
from mena.sessions import session_id
from mena.settings import AppSettings

ExportFormat = Literal["markdown"]


@dataclass(frozen=True)
class ExportCommandInput:
    session_path: Path | None
    summary_artifact_id: str | None = None
    export_format: ExportFormat | None = None


@dataclass(frozen=True)
class ExportWorkflowResult:
    status: Literal["success", "cancelled"]
    export_path: Path | None = None


@dataclass(frozen=True)
class _ResolvedExportSession:
    session_path: Path
    summary_artifact_id: str | None


def run_export_workflow(
    command: ExportCommandInput,
    *,
    settings: AppSettings,
    interaction: InteractionPort | None,
) -> ExportWorkflowResult:
    resolved = resolve_export_session(
        session_path=command.session_path,
        summary_artifact_id=command.summary_artifact_id,
        settings=settings,
    )
    verified = load_verified_session_bundle(resolved.session_path)
    if command.session_path is None and interaction is not None:
        if command.summary_artifact_id is not None:
            interaction.notify(
                WorkflowNotice(
                    topic="export.session_selected.summary_artifact",
                    context={"session_path": resolved.session_path},
                )
            )
        else:
            interaction.notify(
                WorkflowNotice(
                    topic="export.session_selected.latest",
                    context={"session_path": resolved.session_path},
                )
            )

    selected_format = resolve_export_format(
        export_format=command.export_format,
        interaction=interaction,
    )
    if selected_format is None:
        return ExportWorkflowResult(status="cancelled")

    session_record = dict(verified.record)
    session_id_value = session_id(session_record)

    export_result = export_summary_markdown(
        store=FileSystemArtifactStore(
            session_id=session_id_value,
            session_dir=verified.session_dir,
        ),
        summary_record_id=resolved.summary_artifact_id,
    )

    export_path = verified.session_dir / export_result.export_record.path
    if interaction is not None:
        interaction.notify(
            WorkflowNotice(
                topic="export.completed",
                context={"export_path": export_path},
            )
        )
    return ExportWorkflowResult(status="success", export_path=export_path)


def resolve_export_format(
    *,
    export_format: str | None,
    interaction: InteractionPort | None,
) -> ExportFormat | None:
    if export_format is not None:
        if export_format != "markdown":
            raise ValueError(f"Unsupported export format: {export_format}.")
        return "markdown"
    if interaction is None:
        return "markdown"
    confirmed = interaction.confirm(
        ConfirmationRequest(topic="export.format.markdown", default=True)
    )
    return "markdown" if confirmed else None


def resolve_export_session(
    *,
    session_path: Path | None,
    summary_artifact_id: str | None,
    settings: AppSettings,
) -> _ResolvedExportSession:
    if session_path is not None:
        return _ResolvedExportSession(
            session_path=session_path,
            summary_artifact_id=summary_artifact_id,
        )

    selected = resolve_exportable_summary_session(
        sessions_root=settings.paths.sessions_dir,
        summary_artifact_id=summary_artifact_id,
    )
    if selected is None:
        if summary_artifact_id is not None:
            raise ValueError(f"Summary artifact not found: {summary_artifact_id}.")
        raise ValueError("No summarized sessions were found. Run `mena summarize` first.")

    return _ResolvedExportSession(
        session_path=selected.session_dir,
        summary_artifact_id=selected.summary_artifact_id,
    )
