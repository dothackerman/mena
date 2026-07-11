from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import mena.cli as cli
from mena.artifacts import ArtifactRecord
from mena.diarization import DiarizationProviderUnavailable
from mena.interaction import (
    ChoiceOption,
    ChoiceRequest,
    ConfirmationRequest,
    TextRequest,
    WorkflowNotice,
)
from mena.settings import AppSettings
from mena.setup_workflow import SetupMessageEvent, SetupWorkflowResult


def test_console_record_interaction_reports_adapter_notices(
    capsys: pytest.CaptureFixture[str],
) -> None:
    interaction = cli.ConsoleRecordInteraction(lambda _prompt: "q")

    for notice in (
        WorkflowNotice(topic="record.session_created", context={"session_id": "session-1"}),
        WorkflowNotice(topic="record.session_record", context={"record_path": "/tmp/session.json"}),
        WorkflowNotice(topic="record.capture_not_started.discovery_unavailable"),
        WorkflowNotice(topic="record.capture_not_started.error", context={"error": "no input"}),
        WorkflowNotice(topic="record.capture_not_started.noninteractive_stop"),
        WorkflowNotice(topic="record.consent_notice"),
        WorkflowNotice(topic="record.stop_prompt_started"),
        WorkflowNotice(topic="record.stop_prompt_continues"),
        WorkflowNotice(
            topic="record.segment_flushed",
            context={"source_id": "local_mic", "index": 2},
        ),
        WorkflowNotice(
            topic="record.capture_status",
            context={"elapsed_seconds": 7.8, "sealed_segment_count": 3},
        ),
        WorkflowNotice(
            topic="record.capture_route_event",
            context={"event": "recovered", "message": "route restored"},
        ),
        WorkflowNotice(
            topic="record.capture_route_event",
            context={
                "event": "degraded",
                "source_id": "system_audio",
                "code": "dedicated_route_runtime_failed",
                "message": "Selected playback stream ended",
                "selected_sources": ["local_mic", "system_audio"],
            },
        ),
        WorkflowNotice(
            topic="record.capture_route_event",
            context={
                "event": "degraded",
                "source_id": "system_audio",
                "code": "dedicated_route_unavailable",
                "message": "Dedicated route unavailable",
                "selected_sources": ["local_mic", "system_audio"],
            },
        ),
        WorkflowNotice(
            topic="record.capture_route_event",
            context={"event": "lost", "message": "route missing"},
        ),
        WorkflowNotice(topic="record.worker_teardown_interrupted"),
        WorkflowNotice(topic="record.stop_interrupted"),
        WorkflowNotice(topic="record.capture_failed", context={"error": "worker failed"}),
        WorkflowNotice(topic="record.capture_finalized"),
        WorkflowNotice(
            topic="record.process_handoff",
            context={"record_path": "/tmp/session.json"},
        ),
        WorkflowNotice(
            topic="record.finalized_status_error",
            context={"status": "failed", "error": "finalization failed"},
        ),
        WorkflowNotice(topic="record.finalized_status", context={"status": "recorded"}),
    ):
        interaction.notify(notice)

    output = capsys.readouterr().out
    assert "Created session session-1" in output
    assert "Capture recovered: route restored" in output
    assert (
        "Selected playback stream ended during capture; "
        "local microphone capture may have continued. "
        "Selected playback stream ended"
    ) in output
    assert "Capture degraded (system audio): Dedicated route unavailable" in output
    assert "Capture degraded: route missing" in output
    assert "Next command: mena process" in output
    assert "Capture finalized as failed: finalization failed" in output

    with pytest.raises(ValueError, match="Unsupported record notice topic"):
        interaction.notify(WorkflowNotice(topic="record.unknown"))


def test_console_record_interaction_reports_handoff_playback_loss_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    record_path = tmp_path / "session.json"
    record_path.write_text(
        json.dumps(
            {
                "capture_plan": {"selected_sources": ["local_mic", "system_audio"]},
                "capture_metadata": {
                    "sources": [
                        {
                            "source_id": "local_mic",
                            "segment_count": 1,
                        },
                        {
                            "source_id": "system_audio",
                            "segment_count": 1,
                        },
                    ]
                },
                "degradations": [
                    {
                        "event": "degraded",
                        "code": "dedicated_route_runtime_failed",
                        "source_id": "system_audio",
                        "message": "Dedicated meeting route failed during capture.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    interaction = cli.ConsoleRecordInteraction(lambda _prompt: "q")
    interaction.notify(
        WorkflowNotice(
            topic="record.process_handoff",
            context={"record_path": str(record_path)},
        )
    )

    output = capsys.readouterr().out
    assert (
        "Finalization note: selected playback stream ended during recording; "
        "local microphone segments were recorded."
    ) in output


def test_console_record_interaction_handoff_does_not_overstate_unrecorded_mic(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    record_path = tmp_path / "session.json"
    record_path.write_text(
        json.dumps(
            {
                "capture_plan": {"selected_sources": ["local_mic", "system_audio"]},
                "capture_metadata": {
                    "sources": [
                        {
                            "source_id": "system_audio",
                            "segment_count": 1,
                        }
                    ]
                },
                "degradations": [
                    {
                        "event": "degraded",
                        "code": "dedicated_route_runtime_failed",
                        "source_id": "system_audio",
                        "message": "Dedicated meeting route failed during capture.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    interaction = cli.ConsoleRecordInteraction(lambda _prompt: "q")
    interaction.notify(
        WorkflowNotice(
            topic="record.process_handoff",
            context={"record_path": str(record_path)},
        )
    )

    output = capsys.readouterr().out
    assert (
        "Finalization note: selected playback stream ended during recording; "
        "local microphone capture was selected, but no local microphone segments were recorded."
    ) in output


def test_console_record_interaction_reports_playback_loss_without_other_source(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    record_path = tmp_path / "session.json"
    record_path.write_text(
        json.dumps(
            {
                "capture_plan": {"selected_sources": ["system_audio"]},
                "capture_metadata": {
                    "sources": [
                        {
                            "source_id": "system_audio",
                            "segment_count": 1,
                        }
                    ]
                },
                "degradations": [
                    {
                        "event": "degraded",
                        "code": "dedicated_route_runtime_failed",
                        "source_id": "system_audio",
                        "message": "Dedicated meeting route failed during capture.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    interaction = cli.ConsoleRecordInteraction(lambda _prompt: "q")

    interaction.notify(
        WorkflowNotice(
            topic="record.capture_route_event",
            context={
                "event": "degraded",
                "source_id": "system_audio",
                "code": "dedicated_route_runtime_failed",
                "message": "Selected playback stream ended",
                "selected_sources": ["system_audio"],
            },
        )
    )
    interaction.notify(
        WorkflowNotice(
            topic="record.process_handoff",
            context={"record_path": str(record_path)},
        )
    )

    output = capsys.readouterr().out
    assert (
        "Selected playback stream ended during capture; no other selected source was active. "
        "Selected playback stream ended"
    ) in output
    assert (
        "Finalization note: selected playback stream ended during recording; "
        "no other selected source was active."
    ) in output


def test_console_record_interaction_rejects_noninteractive_choices_and_text() -> None:
    interaction = cli.ConsoleRecordInteraction(None)

    with pytest.raises(ValueError, match="pass --mic"):
        interaction.choose(
            ChoiceRequest(
                topic="record.capture_route.microphone",
                options=(ChoiceOption(option_id="mic-1", label="Mic"),),
            )
        )

    with pytest.raises(ValueError, match="pass --playback-stream"):
        interaction.choose(
            ChoiceRequest(
                topic="record.capture_route.playback_stream",
                options=(ChoiceOption(option_id="playback-1", label="Call"),),
            )
        )

    with pytest.raises(
        ValueError,
        match="Multiple meeting audio output routes were discovered; pass --output-sink.",
    ):
        interaction.choose(
            ChoiceRequest(
                topic="record.capture_route.output_sink",
                options=(ChoiceOption(option_id="sink-1", label="Headphones"),),
            )
        )

    with pytest.raises(RuntimeError, match="interactive terminal"):
        interaction.request_text(TextRequest(topic="record.stop_command"))
    with pytest.raises(ValueError, match="Unsupported record confirmation topic"):
        interaction.confirm(ConfirmationRequest(topic="record.unsupported"))
    with pytest.raises(ValueError, match="Unsupported record text topic"):
        cli.ConsoleRecordInteraction(lambda _prompt: "q").request_text(
            TextRequest(topic="record.unsupported")
        )

    with pytest.raises(ValueError, match="Unsupported record choice topic"):
        cli._record_choice_prompt_metadata("record.capture_route.unknown")


def test_prompt_and_runtime_adapter_helpers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    choices = (
        ChoiceOption(option_id="one", label="One"),
        ChoiceOption(option_id="two", label="Two"),
    )

    assert (
        cli._prompt_choice(
            header="Choices:",
            label="choices",
            choices=choices,
            formatter=lambda choice: choice.label,
            input_fn=lambda _prompt: "",
            default_index=2,
        ).option_id
        == "two"
    )
    with pytest.raises(ValueError, match="Invalid default"):
        cli._prompt_choice(
            header="Choices:",
            label="choices",
            choices=choices,
            formatter=lambda choice: choice.label,
            input_fn=lambda _prompt: "1",
            default_index=3,
        )
    with pytest.raises(ValueError, match="Invalid choices selection 'x'"):
        cli._prompt_choice(
            header="Choices:",
            label="choices",
            choices=choices,
            formatter=lambda choice: choice.label,
            input_fn=lambda _prompt: "x",
        )
    with pytest.raises(ValueError, match="Invalid choices selection '3'"):
        cli._prompt_choice(
            header="Choices:",
            label="choices",
            choices=choices,
            formatter=lambda choice: choice.label,
            input_fn=lambda _prompt: "3",
        )

    def explicit(prompt: str) -> str:
        return prompt

    assert cli._record_input(explicit) is explicit
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    assert cli._record_input(None) is input
    assert isinstance(
        cli._resolve_runtime_fallback_interaction(explicit), cli.ConsoleProcessingInteraction
    )
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert cli._resolve_runtime_fallback_interaction(None) is None

    settings = AppSettings.from_env(env={}, home=tmp_path)
    assert cli._effective_record_data_root(tmp_path / "data", settings) == tmp_path / "data"
    assert cli._format_process_handoff_command(tmp_path / "data") == (
        f"MENA_DATA_ROOT={tmp_path / 'data'} mena process"
    )
    assert cli._format_process_handoff_command(None) == "mena process"
    assert cli._resolve_process_session_path(str(tmp_path / "session.json"), settings) == (
        tmp_path / "session.json"
    )
    with pytest.raises(ValueError, match="no recorded sessions"):
        cli._resolve_process_session_path(None, settings)

    cli._print_semantic_review_skip_notice(cast(Any, SimpleNamespace()))
    assert "Semantic review skipped" in capsys.readouterr().out
    assert cli._requires_runtime_profile(None, None, None)
    assert not cli._requires_runtime_profile(lambda: object(), lambda: object(), lambda: object())


def test_record_choice_and_data_root_adapter_branches(
    tmp_path: Path,
) -> None:
    prompts: list[str] = []

    def _input(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    interaction = cli.ConsoleRecordInteraction(_input)
    selected = interaction.choose(
        ChoiceRequest(
            topic="record.capture_route.output_sink",
            options=(
                ChoiceOption(option_id="one", label="One"),
                ChoiceOption(option_id="two", label="Two"),
            ),
            default_option_id="two",
        )
    )

    assert selected.option_id == "two"
    assert prompts == ["Select meeting audio output [2]: "]
    assert cli._record_choice_prompt_metadata("record.capture_route.playback_stream") == (
        "playback stream",
        "--playback-stream",
        "Playback streams:",
        "playback streams",
    )
    assert cli._record_choice_prompt_metadata("record.capture_route.output_sink") == (
        "meeting audio output",
        "--output-sink",
        "Where do you hear the meeting?",
        "meeting audio output",
    )

    invalid_selection = cli.ConsoleRecordInteraction(lambda _prompt: "x")
    with pytest.raises(ValueError, match="Invalid meeting audio output selection 'x'"):
        invalid_selection.choose(
            ChoiceRequest(
                topic="record.capture_route.output_sink",
                options=(
                    ChoiceOption(option_id="one", label="One"),
                    ChoiceOption(option_id="two", label="Two"),
                ),
                default_option_id=None,
            )
        )
    assert cli._resolve_record_profile(None, input_fn=lambda _prompt: "2").id.value == (
        "in_person_single_mic"
    )

    xdg_home = tmp_path / "xdg"
    settings = AppSettings.from_env(env={"XDG_DATA_HOME": str(xdg_home)}, home=tmp_path)

    assert cli._effective_record_data_root(None, settings) is None


def test_processing_and_merged_transcript_adapter_edge_branches(tmp_path: Path) -> None:
    artifact = SimpleNamespace(id="artifact-1")
    failed_state = SimpleNamespace(
        status="failed",
        input_artifacts=(artifact,),
    )
    ledger = SimpleNamespace(latest=lambda _stage: failed_state)

    assert cli._latest_failed_stage_input_artifacts(
        ledger=cast(Any, ledger),
        stage="transcription",
    ) == ("artifact-1",)
    assert (
        cli._latest_failed_stage_input_artifacts(
            ledger=cast(Any, SimpleNamespace(latest=lambda _stage: None)),
            stage="transcription",
        )
        == ()
    )
    assert (
        cli._runtime_fallback_stage_for_provider_failure(DiarizationProviderUnavailable("gpu"))
        == "diarization"
    )

    invalid_profile = SimpleNamespace(
        sources=(
            SimpleNamespace(logical_id=SimpleNamespace(value="local_mic")),
            SimpleNamespace(logical_id=SimpleNamespace(value="extra_source")),
        )
    )
    with pytest.raises(ValueError, match="Processing currently supports"):
        cli._validate_processing_profile_support(cast(Any, invalid_profile))

    record = cast(ArtifactRecord, SimpleNamespace(id="merged-1", path="merged.json"))
    unverifiable_store = SimpleNamespace(verify=lambda _record_id: SimpleNamespace(ok=False))
    assert (
        cli._merged_transcript_timestamp_key(
            store=cast(Any, unverifiable_store),
            record=record,
        )
        is None
    )
    assert cli._parse_iso_timestamp("") is None
    assert cli._parse_iso_timestamp("not-a-date") is None
    assert cli._parse_iso_timestamp("2026-05-19T15:20:00Z") is not None

    empty_store = SimpleNamespace(list=lambda **_kwargs: ())
    assert cli._resolve_latest_merged_transcript_record(store=cast(Any, empty_store)) is None


def test_main_reports_adapter_cancellations_and_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)

    assert (
        cli.main(
            ["record"], settings=settings, input_fn=lambda _prompt: (_ for _ in ()).throw(EOFError)
        )
        == 1
    )
    assert "Recording setup cancelled." in capsys.readouterr().out

    monkeypatch.setattr(
        cli,
        "_run_summarize_command",
        lambda **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    assert cli.main(["summarize", str(tmp_path)], settings=settings) == 1
    assert "Summarization cancelled." in capsys.readouterr().out

    monkeypatch.setattr(
        cli,
        "_run_inspect_command",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("manifest missing")),
    )
    assert cli.main(["inspect", str(tmp_path)], settings=settings) == 1
    assert "Cannot inspect session: manifest missing" in capsys.readouterr().out

    monkeypatch.setattr(
        cli,
        "_run_processing_command",
        lambda **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    assert cli.main(["process", str(tmp_path)], settings=settings) == 1
    assert "Processing workflow interrupted." in capsys.readouterr().out


def test_console_correction_and_processing_reject_unsupported_topics() -> None:
    correction = cli.ConsoleCorrectionInteraction(lambda _prompt: "no")
    processing_interaction = cli.ConsoleProcessingInteraction(lambda _prompt: "no")

    with pytest.raises(ValueError, match="Unsupported correction choice topic"):
        correction.choose(
            ChoiceRequest(
                topic="correction.unsupported",
                options=(ChoiceOption(option_id="skip", label="Skip"),),
            )
        )
    with pytest.raises(ValueError, match="Unsupported correction confirmation topic"):
        correction.confirm(ConfirmationRequest(topic="correction.unsupported"))
    assert not correction.confirm(ConfirmationRequest(topic="correction.overlap_mark"))
    with pytest.raises(ValueError, match="Unsupported correction text topic"):
        correction.request_text(TextRequest(topic="correction.unsupported"))
    assert correction.request_text(TextRequest(topic="correction.text_edit")) == "no"
    with pytest.raises(ValueError, match="Unsupported correction notice topic"):
        correction.notify(WorkflowNotice(topic="correction.unsupported"))

    assert cli._correction_action_option_id("play") == "playback"
    assert cli._correction_action_option_id("reassign") == "speaker_reassignment"
    assert cli._correction_action_option_id("text") == "text_edit"
    assert cli._correction_action_option_id("overlap") == "overlap_mark"
    assert cli._correction_action_option_id("?") is None

    with pytest.raises(ValueError, match="Unsupported processing choice topic"):
        processing_interaction.choose(
            ChoiceRequest(
                topic="processing.unsupported",
                options=(ChoiceOption(option_id="skip", label="Skip"),),
            )
        )
    with pytest.raises(ValueError, match="Unsupported processing text topic"):
        processing_interaction.request_text(TextRequest(topic="processing.unsupported"))
    with pytest.raises(ValueError, match="Unsupported processing notice topic"):
        processing_interaction.notify(WorkflowNotice(topic="processing.unsupported"))


def test_setup_cli_interaction_and_command_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    interaction = cli._CliSetupInteraction(lambda _prompt: "yes")

    assert interaction.confirm(ConfirmationRequest(topic="setup.primary_actions"))
    assert interaction.confirm(ConfirmationRequest(topic="setup.fallback_actions"))
    with pytest.raises(ValueError, match="Unsupported setup confirmation request"):
        interaction.confirm(ConfirmationRequest(topic="setup.unknown"))
    with pytest.raises(ValueError, match="Unsupported setup choice request"):
        interaction.choose(
            ChoiceRequest(
                topic="setup.choice",
                options=(ChoiceOption(option_id="one", label="One"),),
            )
        )
    with pytest.raises(ValueError, match="Unsupported setup text request"):
        interaction.request_text(TextRequest(topic="setup.text"))
    interaction.notify(WorkflowNotice(topic="setup.notice"))

    def _run_setup_workflow(**_kwargs: object) -> SetupWorkflowResult:
        return SetupWorkflowResult(
            exit_code=7,
            events=(SetupMessageEvent("setup message"),),
        )

    monkeypatch.setattr(cli, "run_setup_workflow", _run_setup_workflow)

    assert (
        cli._run_setup_command(
            check_only=False,
            assume_yes=True,
            authorize_local_fallback=False,
            settings=AppSettings.from_env(env={}, home=tmp_path),
            input_fn=lambda _prompt: "yes",
            runner=None,
            command_identity=None,
            hardware=None,
        )
        == 7
    )
    assert "setup message" in capsys.readouterr().out


def test_cli_session_field_and_asr_language_adapters() -> None:
    # Adapter-local helpers with no delegation to the processing workflow
    # modules; unreachable through build_parser/main, so covered directly.
    assert cli._asr_language_from_session_language("de-CH") == "de"
    assert cli._asr_language_from_session_language(None) is None
    assert cli._required_str({"id": "session"}, "id") == "session"
    assert cli._required_int({"count": 2}, "count") == 2
    assert cli._optional_metadata_str({"route": None}, "route") is None
    with pytest.raises(ValueError, match="required"):
        cli._required_str(object(), "id")
    with pytest.raises(ValueError, match="positive integer"):
        cli._required_int({"count": 0}, "count")
