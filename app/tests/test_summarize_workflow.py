import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from mena.artifacts import ArtifactStore, FileSystemArtifactStore, InMemoryArtifactStore
from mena.interaction import (
    ChoiceRequest,
    ConfirmationRequest,
    ScriptedInteraction,
    ScriptedResponse,
)
from mena.merge_timing import MERGED_TRANSCRIPT_ARTIFACT_TYPE
from mena.mistral_llm import MistralSummaryState
from mena.runtime_profile import RuntimeProfileSelection, SummarizationRuntimePlan
from mena.sessions import resolve_session, session_language_or_default
from mena.settings import AppSettings
from mena.setup import StageProfile, mistral_summary_stage_profile
from mena.summarization import (
    SummarizationError,
    SummarizationPolicy,
    SummarizationProviderUnavailable,
)
from mena.summarize_workflow import (
    _merged_transcript_timestamp_key,
    _parse_iso_timestamp,
    _resolve_latest_merged_transcript_record,
    resolve_summarize_session_path,
    run_summarization_command,
)
from test_cli import (
    _build_ready_summarize_session_bundle_with_metadata,
    _build_summarize_session_bundle,
)

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


def test_summarize_workflow_requests_semantic_choice_for_multiple_ready_sessions(
    tmp_path: Path,
) -> None:
    _build_ready_summarize_session_bundle_with_metadata(
        tmp_path,
        directory_name="older",
        created_at="2026-05-19T12:00:00Z",
        name="Older Ready Session",
    )
    newer = _build_ready_summarize_session_bundle_with_metadata(
        tmp_path,
        directory_name="newer",
        created_at="2026-05-19T13:00:00Z",
        name="Newest Ready Session",
    )
    interaction = ScriptedInteraction(
        [ScriptedResponse(kind="choose", topic="summarize.session", value=None)]
    )

    selected = resolve_summarize_session_path(
        explicit_session_path=None,
        sessions_root=tmp_path / "sessions",
        interaction=interaction,
    )

    assert selected == newer
    request = cast(ChoiceRequest, interaction.requests[0])
    assert request.topic == "summarize.session"
    assert request.default_option_id == newer.as_posix()
    assert [option.label for option in request.options] == [
        "newer (Newest Ready Session, 2026-05-19T13:00:00+00:00)",
        "older (Older Ready Session, 2026-05-19T12:00:00+00:00)",
    ]
    assert interaction.notices[-1].topic == "summarize.session_selected"
    assert interaction.notices[-1].context["session_dir"] == newer


def test_summarize_workflow_notifies_progress_and_export_guidance(
    tmp_path: Path,
) -> None:
    session_dir, _, _, _ = _build_summarize_session_bundle(tmp_path)
    interaction = ScriptedInteraction()

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
            interaction=interaction,
        )
        == 0
    )

    assert [notice.topic for notice in interaction.notices] == [
        "summarize.started",
        "summarize.completed",
    ]
    assert interaction.notices[-1].context["summary_path"] == "artifacts/summary/summary/final.json"
    assert interaction.notices[-1].context["summary_artifact_id"] == (
        "00000000-0000-0000-0000-000000000001:artifacts/summary/summary/final.json"
    )
    assert interaction.notices[-1].context["session_dir"] == session_dir


def test_summarize_workflow_prints_quoted_export_command_for_returned_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_root = tmp_path / "Mena Data"
    data_root.mkdir()
    session_dir, _, _, _ = _build_summarize_session_bundle(data_root)
    summary_id = ""

    def _stage(**kwargs: object) -> SimpleNamespace:
        nonlocal summary_id
        store = cast(ArtifactStore, kwargs["store"])
        summary_record = store.store(
            artifact_type="summary",
            name="summary/final.json",
            data=b"{}",
            version=1,
        )
        summary_id = summary_record.id
        return SimpleNamespace(summary_record=summary_record)

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
        )
        == 0
    )

    assert (
        f"Next command: mena export --summary-artifact-id {summary_id} '{session_dir}'"
        in capsys.readouterr().out
    )


def test_summarize_workflow_reports_no_ready_sessions_for_unlistable_root(
    tmp_path: Path,
) -> None:
    sessions_root = tmp_path / "sessions"
    sessions_root.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="No ready sessions were found"):
        resolve_summarize_session_path(
            explicit_session_path=None,
            sessions_root=sessions_root,
        )


def test_summarize_workflow_skips_session_with_unreadable_artifact_manifest(
    tmp_path: Path,
) -> None:
    _build_ready_summarize_session_bundle_with_metadata(
        tmp_path,
        directory_name="bad",
        created_at="2026-05-19T14:00:00Z",
        name="Unreadable Manifest",
    )
    valid_session = _build_ready_summarize_session_bundle_with_metadata(
        tmp_path,
        directory_name="valid",
        created_at="2026-05-19T13:00:00Z",
        name="Valid Ready Session",
    )
    manifest_path = tmp_path / "sessions" / "bad" / "artifacts" / "artifact-manifest.v1.json"
    manifest_path.unlink()
    manifest_path.mkdir()

    selected = resolve_summarize_session_path(
        explicit_session_path=None,
        sessions_root=tmp_path / "sessions",
    )

    assert selected == valid_session


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


def _fallback_plan() -> SummarizationRuntimePlan:
    accepted_stage = StageProfile(
        stage="summarization",
        provider="Ollama",
        model="llama3.1:8b",
        device="local-service",
        policy="local-preferred",
    )
    return SummarizationRuntimePlan(
        accepted_stage=accepted_stage,
        actual_stage=accepted_stage,
        ordered_fallback=(mistral_summary_stage_profile(),),
    )


def test_summarize_workflow_provokes_fallback_to_the_next_accepted_provider(
    tmp_path: Path,
) -> None:
    session_dir, _, _, _ = _build_summarize_session_bundle(tmp_path)
    plan = _fallback_plan()
    primary_selection = cast(
        RuntimeProfileSelection,
        SimpleNamespace(accepted="accepted-state", summarization=plan),
    )
    fallback_selection = cast(
        RuntimeProfileSelection,
        SimpleNamespace(
            accepted="accepted-state",
            summarization=replace(plan, actual_stage=plan.ordered_fallback[0]),
        ),
    )
    stage_calls: list[dict[str, object]] = []
    store_calls: list[dict[str, object]] = []
    bind_calls: list[object] = []
    fallback_kwargs: dict[str, object] = {}
    release_calls: list[tuple[object, int]] = []

    def _stage(**kwargs: object) -> SimpleNamespace:
        stage_calls.append(kwargs)
        if len(stage_calls) == 1:
            raise SummarizationProviderUnavailable("Ollama summarization provider is unavailable.")
        store = cast(ArtifactStore, kwargs["store"])
        return SimpleNamespace(
            summary_record=store.store(
                artifact_type="summary",
                name="summary/final.json",
                data=b"{}",
                version=1,
            ),
        )

    def _fallback_selector(**kwargs: object) -> RuntimeProfileSelection:
        fallback_kwargs.update(kwargs)
        return fallback_selection

    def _bind(**kwargs: object) -> SimpleNamespace:
        bind_calls.append(kwargs["selection"])
        return SimpleNamespace(label=f"provider-{len(bind_calls)}")

    def _release(**kwargs: object) -> None:
        release_calls.append((kwargs["provider"], cast(int, kwargs["attempt"])))

    exit_code = run_summarization_command(
        session_path=session_dir,
        manual_notes=None,
        language=None,
        settings=AppSettings.from_env(env={}, home=tmp_path),
        runtime_profile_selector_fn=lambda **_kwargs: primary_selection,
        runtime_profile_store_fn=lambda **kwargs: store_calls.append(kwargs),
        runtime_summarization_binder_fn=_bind,
        run_summarization_stage_fn=_stage,
        summarization_stage_fallback_selector_fn=_fallback_selector,
        terminal_llm_release_fn=_release,
    )

    assert exit_code == 0
    assert len(stage_calls) == 2
    assert stage_calls[0]["attempt"] == 1
    assert stage_calls[1]["attempt"] == 2
    assert bind_calls == [primary_selection, fallback_selection]
    assert fallback_kwargs["accepted"] == "accepted-state"
    assert fallback_kwargs["used_plan"] is plan
    assert (
        cast(str, fallback_kwargs["failure_reason"])
        == "Ollama summarization provider is unavailable."
    )
    assert fallback_kwargs["failure_class"] == "SummarizationProviderUnavailable"
    assert len(cast(tuple[str, ...], fallback_kwargs["failed_input_artifacts"])) >= 2
    assert [call.get("name_suffix") for call in store_calls] == [None, "stage-fallback"]
    # The failed primary provider is released before switching, and the
    # fallback provider is released at the end.
    assert [attempt for _provider, attempt in release_calls] == [1, 2]
    assert release_calls[0][0] is not release_calls[1][0]


def test_summarize_workflow_reraises_when_no_summarization_plan_is_available(
    tmp_path: Path,
) -> None:
    session_dir, _, _, _ = _build_summarize_session_bundle(tmp_path)

    def _stage(**_kwargs: object) -> SimpleNamespace:
        raise SummarizationProviderUnavailable("Ollama summarization provider is unavailable.")

    with pytest.raises(SummarizationProviderUnavailable):
        run_summarization_command(
            session_path=session_dir,
            manual_notes=None,
            language=None,
            settings=AppSettings.from_env(env={}, home=tmp_path),
            runtime_profile_selector_fn=_runtime_selection,
            runtime_profile_store_fn=lambda **_kwargs: None,
            runtime_summarization_binder_fn=lambda **_kwargs: SimpleNamespace(),
            run_summarization_stage_fn=_stage,
            summarization_stage_fallback_selector_fn=lambda **_kwargs: pytest.fail(
                "no fallback selection without a summarization plan"
            ),
        )


def test_summarize_workflow_remote_fallback_authorizer_uses_the_interaction_port(
    tmp_path: Path,
) -> None:
    session_dir, _, _, _ = _build_summarize_session_bundle(tmp_path)
    interaction = ScriptedInteraction(
        [
            ScriptedResponse(
                kind="confirm",
                topic="runtime_fallback.summarization_remote",
                value=True,
            )
        ]
    )
    probe_state = MistralSummaryState(
        key_present=True,
        reachable=True,
        request_model="mistral-small-latest",
        resolved_model="mistral-small-2603",
        model_allowed=True,
        policy="mena-mistral-summary-allowlist-2026-06-29-v1",
        detail="resolved inside the accepted snapshot",
    )
    authorized: dict[str, object] = {}

    def _selector(**kwargs: object) -> RuntimeProfileSelection:
        authorize = cast(
            Callable[[str, MistralSummaryState], bool],
            kwargs["authorize_remote_summarization_fallback"],
        )
        authorized["result"] = authorize("ollama is down", probe_state)
        return _runtime_selection()

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
            runtime_profile_selector_fn=_selector,
            runtime_profile_store_fn=lambda **_kwargs: None,
            runtime_summarization_binder_fn=lambda **_kwargs: SimpleNamespace(),
            run_summarization_stage_fn=_stage,
            interaction=interaction,
        )
        == 0
    )

    assert authorized["result"] is True
    request = cast(ConfirmationRequest, interaction.requests[0])
    assert request.topic == "runtime_fallback.summarization_remote"
    assert request.context["reason"] == "ollama is down"
    assert request.context["resolved_model"] == "mistral-small-2603"
    assert isinstance(request.context["disclosure"], tuple)


def test_summarize_workflow_remote_fallback_authorizer_declines_without_interaction(
    tmp_path: Path,
) -> None:
    from mena.summarize_workflow import _remote_fallback_authorizer

    authorize = _remote_fallback_authorizer(None)
    probe_state = MistralSummaryState(
        key_present=True,
        reachable=True,
        request_model="mistral-small-latest",
        resolved_model="mistral-small-2603",
        model_allowed=True,
        policy="mena-mistral-summary-allowlist-2026-06-29-v1",
        detail="resolved inside the accepted snapshot",
    )

    assert authorize("ollama is down", probe_state) is False
