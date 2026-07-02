import pytest

from mena.interaction import (
    ChoiceOption,
    ChoiceRequest,
    ConfirmationRequest,
    InteractionContractError,
    InteractionPort,
    ScriptedInteraction,
    ScriptedInteractionError,
    ScriptedResponse,
    TextRequest,
    WorkflowNotice,
)


def _route_choice(default: str | None = "route-1") -> ChoiceRequest:
    return ChoiceRequest(
        topic="record.capture_route.microphone",
        options=(
            ChoiceOption(option_id="route-1", label="Headset Microphone (USB)"),
            ChoiceOption(option_id="route-2", label="Laptop Microphone", detail="node 42"),
        ),
        default_option_id=default,
    )


def test_choice_request_rejects_empty_options() -> None:
    with pytest.raises(InteractionContractError, match="at least one option"):
        ChoiceRequest(topic="record.capture_route.microphone", options=())


def test_choice_request_rejects_duplicate_option_ids() -> None:
    option = ChoiceOption(option_id="route-1", label="Headset")
    with pytest.raises(InteractionContractError, match="duplicate option ids"):
        ChoiceRequest(topic="record.capture_route.microphone", options=(option, option))


def test_choice_request_rejects_default_outside_options() -> None:
    with pytest.raises(InteractionContractError, match="not one of its options"):
        _route_choice(default="route-9")


def test_requests_reject_non_lowercase_topics() -> None:
    with pytest.raises(InteractionContractError, match="lowercase identifier"):
        TextRequest(topic="Correction.Text_Edit")


def test_notice_rejects_invalid_severity() -> None:
    with pytest.raises(InteractionContractError, match="invalid severity"):
        WorkflowNotice(topic="record.heartbeat", severity="fatal")  # type: ignore[arg-type]


def test_scripted_choose_returns_option_and_records_request() -> None:
    interaction = ScriptedInteraction(
        [ScriptedResponse(kind="choose", topic="record.capture_route.microphone", value="route-2")]
    )

    chosen = interaction.choose(_route_choice())

    assert chosen.option_id == "route-2"
    assert [request.topic for request in interaction.requests] == [
        "record.capture_route.microphone"
    ]
    assert interaction.pending == 0


def test_scripted_choose_none_takes_request_default() -> None:
    interaction = ScriptedInteraction(
        [ScriptedResponse(kind="choose", topic="record.capture_route.microphone")]
    )

    assert interaction.choose(_route_choice()).option_id == "route-1"


def test_scripted_choose_none_without_default_fails() -> None:
    interaction = ScriptedInteraction(
        [ScriptedResponse(kind="choose", topic="record.capture_route.microphone")]
    )

    with pytest.raises(ScriptedInteractionError, match="no default option"):
        interaction.choose(_route_choice(default=None))


def test_scripted_choose_rejects_unknown_option_id() -> None:
    interaction = ScriptedInteraction(
        [ScriptedResponse(kind="choose", topic="record.capture_route.microphone", value="route-9")]
    )

    with pytest.raises(InteractionContractError, match="no option 'route-9'"):
        interaction.choose(_route_choice())


def test_scripted_confirm_and_text_return_scripted_values() -> None:
    interaction = ScriptedInteraction(
        [
            ScriptedResponse(kind="confirm", topic="runtime_fallback.one_run", value=True),
            ScriptedResponse(kind="text", topic="correction.text_edit", value="Fixed wording"),
        ]
    )

    assert interaction.confirm(ConfirmationRequest(topic="runtime_fallback.one_run")) is True
    assert interaction.request_text(TextRequest(topic="correction.text_edit")) == "Fixed wording"
    assert interaction.pending == 0


def test_scripted_confirm_rejects_non_bool_value() -> None:
    interaction = ScriptedInteraction(
        [ScriptedResponse(kind="confirm", topic="runtime_fallback.one_run", value="yes")]
    )

    with pytest.raises(ScriptedInteractionError, match="must be a bool"):
        interaction.confirm(ConfirmationRequest(topic="runtime_fallback.one_run"))


def test_scripted_interaction_fails_on_topic_mismatch() -> None:
    interaction = ScriptedInteraction(
        [ScriptedResponse(kind="confirm", topic="setup.fallback_authorization", value=True)]
    )

    with pytest.raises(ScriptedInteractionError, match="script expects confirm"):
        interaction.confirm(ConfirmationRequest(topic="runtime_fallback.one_run"))


def test_scripted_interaction_fails_when_script_exhausted() -> None:
    interaction = ScriptedInteraction()

    with pytest.raises(ScriptedInteractionError, match="script exhausted"):
        interaction.request_text(TextRequest(topic="correction.text_edit"))


def test_notices_are_recorded_without_consuming_script() -> None:
    interaction = ScriptedInteraction(
        [ScriptedResponse(kind="confirm", topic="runtime_fallback.one_run", value=False)]
    )

    interaction.notify(WorkflowNotice(topic="record.degradation", severity="warning"))

    assert [notice.topic for notice in interaction.notices] == ["record.degradation"]
    assert interaction.pending == 1


def test_scripted_interaction_satisfies_the_port() -> None:
    interaction: InteractionPort = ScriptedInteraction()

    interaction.notify(WorkflowNotice(topic="record.heartbeat"))
