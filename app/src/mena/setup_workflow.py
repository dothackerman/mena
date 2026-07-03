"""Application workflow for Linux alpha setup."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from mena.interaction import ConfirmationRequest, InteractionPort
from mena.settings import AppSettings
from mena.setup import (
    CommandRunner,
    RuntimeProfileState,
    SetupActionResult,
    SetupEvaluation,
    SubprocessCommandRunner,
    build_runtime_profile_state,
    default_profile_catalog,
    evaluate_setup,
    execute_setup_plan,
    format_setup_report,
    get_alpha_local_profile,
    persist_runtime_profile_state,
)
from mena.setup_detection import DefaultSetupDetector, SetupDetector


@dataclass(frozen=True)
class SetupWorkflowOptions:
    """User-selected setup command options."""

    check_only: bool = False
    assume_yes: bool = False
    authorize_local_fallback: bool = False


@dataclass(frozen=True)
class SetupReportEvent:
    """A setup report to render for the user."""

    evaluation: SetupEvaluation
    action_results: tuple[SetupActionResult, ...] = ()
    final_ready: bool | None = None

    def render(self) -> str:
        return format_setup_report(
            self.evaluation,
            action_results=self.action_results,
            final_ready=self.final_ready,
        )


@dataclass(frozen=True)
class SetupMessageEvent:
    """A user-visible setup workflow message."""

    text: str


SetupWorkflowEvent = SetupReportEvent | SetupMessageEvent


@dataclass(frozen=True)
class SetupWorkflowResult:
    """Workflow result rendered by an interface adapter."""

    exit_code: int
    events: tuple[SetupWorkflowEvent, ...]
    state_path: Path | None = None
    accepted_state: RuntimeProfileState | None = None


def run_setup_workflow(
    *,
    settings: AppSettings,
    options: SetupWorkflowOptions,
    interaction: InteractionPort,
    runner: CommandRunner | None = None,
    detector: SetupDetector | None = None,
) -> SetupWorkflowResult:
    """Run setup evaluation, optional mutation, fallback authorization, and persistence."""

    command_runner = runner or SubprocessCommandRunner()
    setup_detector = detector or DefaultSetupDetector()
    events: list[SetupWorkflowEvent] = []
    profile = get_alpha_local_profile(settings.local_llm.ollama_model)

    try:
        state = setup_detector.detect(settings, runner=command_runner)
    except KeyboardInterrupt:
        return _interrupted(events)

    evaluation = evaluate_setup(profile, state)
    events.append(SetupReportEvent(evaluation))

    if options.check_only:
        return SetupWorkflowResult(exit_code=0 if evaluation.ready else 1, events=tuple(events))

    if evaluation.ready:
        return _persist_ready_setup(
            settings=settings,
            options=options,
            interaction=interaction,
            runner=command_runner,
            detector=setup_detector,
            primary_evaluation=evaluation,
            events=events,
        )

    if not evaluation.plan.actions:
        return SetupWorkflowResult(exit_code=1, events=tuple(events))
    if _readiness_item_failed(evaluation.readiness, "command:mena"):
        events.append(
            SetupMessageEvent(
                "Setup actions were not run because this is not the installed product command."
            )
        )
        return SetupWorkflowResult(exit_code=1, events=tuple(events))
    if not any(action.mode == "automated" for action in evaluation.plan.actions):
        events.append(
            SetupMessageEvent("Setup actions were not run because only manual actions remain.")
        )
        return SetupWorkflowResult(exit_code=1, events=tuple(events))

    if not options.assume_yes:
        try:
            if not interaction.confirm(
                ConfirmationRequest(topic="setup.primary_actions", default=False)
            ):
                events.append(
                    SetupMessageEvent("Setup declined; local provider readiness is incomplete.")
                )
                return SetupWorkflowResult(exit_code=1, events=tuple(events))
        except (EOFError, KeyboardInterrupt):
            return _interrupted(events)

    try:
        results = execute_setup_plan(evaluation.plan, runner=command_runner)
    except KeyboardInterrupt:
        return _interrupted(events)
    try:
        refreshed_state = setup_detector.detect(settings, runner=command_runner)
    except KeyboardInterrupt:
        return _interrupted(events)

    refreshed_evaluation = evaluate_setup(profile, refreshed_state)
    final_ready = refreshed_evaluation.ready
    events.append(
        SetupReportEvent(
            refreshed_evaluation,
            action_results=results,
            final_ready=final_ready,
        )
    )
    if not final_ready:
        return SetupWorkflowResult(exit_code=1, events=tuple(events))

    return _persist_ready_setup(
        settings=settings,
        options=options,
        interaction=interaction,
        runner=command_runner,
        detector=setup_detector,
        primary_evaluation=refreshed_evaluation,
        events=events,
    )


def _persist_ready_setup(
    *,
    settings: AppSettings,
    options: SetupWorkflowOptions,
    interaction: InteractionPort,
    runner: CommandRunner,
    detector: SetupDetector,
    primary_evaluation: SetupEvaluation,
    events: list[SetupWorkflowEvent],
) -> SetupWorkflowResult:
    try:
        fallback_evaluation = _evaluate_fallback_authorization(
            settings=settings,
            runner=runner,
            detector=detector,
        )
        fallback_evaluation = _maybe_execute_fallback_actions(
            fallback_evaluation=fallback_evaluation,
            options=options,
            interaction=interaction,
            settings=settings,
            runner=runner,
            detector=detector,
            events=events,
        )
    except KeyboardInterrupt:
        return _interrupted(events)
    accepted_state = build_runtime_profile_state(
        primary_evaluation,
        fallback_evaluation=fallback_evaluation,
        authorize_local_fallback=options.authorize_local_fallback,
    )
    state_path = persist_runtime_profile_state(accepted_state, paths=settings.paths)
    _append_runtime_fallback_authorization_notice(events, accepted_state)
    events.append(SetupMessageEvent(f"Accepted runtime profile state: {state_path}"))
    return SetupWorkflowResult(
        exit_code=0,
        events=tuple(events),
        state_path=state_path,
        accepted_state=accepted_state,
    )


def _evaluate_fallback_authorization(
    *,
    settings: AppSettings,
    runner: CommandRunner,
    detector: SetupDetector,
) -> SetupEvaluation | None:
    fallback_profile = next(
        (
            profile
            for profile in default_profile_catalog(settings.local_llm.ollama_model)
            if profile.fallback
        ),
        None,
    )
    if fallback_profile is None:
        return None
    fallback_state = detector.detect(settings, runner=runner, profile=fallback_profile)
    return evaluate_setup(fallback_profile, fallback_state)


def _maybe_execute_fallback_actions(
    *,
    fallback_evaluation: SetupEvaluation | None,
    options: SetupWorkflowOptions,
    interaction: InteractionPort,
    settings: AppSettings,
    runner: CommandRunner,
    detector: SetupDetector,
    events: list[SetupWorkflowEvent],
) -> SetupEvaluation | None:
    if (
        fallback_evaluation is None
        or not options.authorize_local_fallback
        or fallback_evaluation.ready
        or not any(action.mode == "automated" for action in fallback_evaluation.plan.actions)
    ):
        return fallback_evaluation

    if not options.assume_yes:
        try:
            confirmed = interaction.confirm(
                ConfirmationRequest(topic="setup.fallback_actions", default=False)
            )
        except EOFError:
            events.append(
                SetupMessageEvent(
                    "Fallback setup actions declined; automatic local runtime fallback "
                    "remains unavailable."
                )
            )
            return fallback_evaluation
        if not confirmed:
            events.append(
                SetupMessageEvent(
                    "Fallback setup actions declined; automatic local runtime fallback "
                    "remains unavailable."
                )
            )
            return fallback_evaluation

    results = execute_setup_plan(fallback_evaluation.plan, runner=runner)
    refreshed_fallback_state = detector.detect(
        settings,
        runner=runner,
        profile=fallback_evaluation.profile,
    )
    refreshed_fallback_evaluation = evaluate_setup(
        fallback_evaluation.profile,
        refreshed_fallback_state,
    )
    events.append(
        SetupReportEvent(
            refreshed_fallback_evaluation,
            action_results=results,
            final_ready=refreshed_fallback_evaluation.ready,
        )
    )
    return refreshed_fallback_evaluation


def _append_runtime_fallback_authorization_notice(
    events: list[SetupWorkflowEvent],
    state: RuntimeProfileState,
) -> None:
    authorization = state.fallback_authorization
    if authorization.local_status == "authorized":
        stages = ", ".join(authorization.stage_scope)
        events.append(
            SetupMessageEvent(f"Automatic local runtime fallback authorized for: {stages}.")
        )
    elif authorization.local_status == "configured_unauthorized":
        events.append(
            SetupMessageEvent(
                "Automatic local runtime fallback is not authorized; rerun `mena setup "
                "--authorize-local-fallback` to permit setup-evaluated local fallback."
            )
        )
    elif authorization.local_status == "unavailable":
        events.append(
            SetupMessageEvent(
                "Automatic local runtime fallback is unavailable and remains disabled."
            )
        )
        for reason in authorization.blocking_reasons:
            events.append(SetupMessageEvent(f"Fallback blocker: {reason}"))


def _interrupted(events: list[SetupWorkflowEvent]) -> SetupWorkflowResult:
    events.append(SetupMessageEvent("Setup interrupted; local provider readiness is incomplete."))
    return SetupWorkflowResult(exit_code=1, events=tuple(events))


def _readiness_item_failed(readiness: Sequence[object], item_id: str) -> bool:
    return any(
        getattr(item, "id", None) == item_id and not getattr(item, "ready", False)
        for item in readiness
    )
