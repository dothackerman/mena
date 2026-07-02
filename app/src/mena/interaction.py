"""Interaction port: the seam where application workflow modules meet the user.

Workflow modules ask for decisions (choices, confirmations, text) and report
notices through this port, expressed semantically. The workflow owns which
decision is needed and when; the interface adapter owns prompt wording,
rendering, and input mechanics (see CONTEXT.md "Interaction port" and ADR-002).

Topics are stable, lowercase, dot-separated machine identifiers (for example
"record.capture_route.microphone" or "runtime_fallback.one_run"). Adapters map
topics to user-facing wording; workflows and tests match on them. Option labels
and context values are domain data (device names, model identifiers, counts),
never adapter wording.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol


class InteractionError(RuntimeError):
    """Base error for interaction-port failures."""


class InteractionContractError(InteractionError):
    """An interaction request violates the port contract."""


class ScriptedInteractionError(InteractionError):
    """The scripted test adapter received a request it was not scripted for."""


@dataclass(frozen=True)
class ChoiceOption:
    """One selectable option; identifier and label are domain data."""

    option_id: str
    label: str
    detail: str | None = None

    def __post_init__(self) -> None:
        if not self.option_id:
            raise InteractionContractError("Choice option requires a non-empty option_id.")
        if not self.label:
            raise InteractionContractError(
                f"Choice option '{self.option_id}' requires a non-empty label."
            )


@dataclass(frozen=True)
class ChoiceRequest:
    """Ask the user to select exactly one option for a topic."""

    topic: str
    options: tuple[ChoiceOption, ...]
    default_option_id: str | None = None

    def __post_init__(self) -> None:
        _validate_topic(self.topic)
        if not self.options:
            raise InteractionContractError(
                f"Choice request '{self.topic}' requires at least one option."
            )
        option_ids = [option.option_id for option in self.options]
        if len(set(option_ids)) != len(option_ids):
            raise InteractionContractError(
                f"Choice request '{self.topic}' has duplicate option ids."
            )
        if self.default_option_id is not None and self.default_option_id not in option_ids:
            raise InteractionContractError(
                f"Choice request '{self.topic}' default '{self.default_option_id}'"
                " is not one of its options."
            )

    def option(self, option_id: str) -> ChoiceOption:
        for candidate in self.options:
            if candidate.option_id == option_id:
                return candidate
        raise InteractionContractError(
            f"Choice request '{self.topic}' has no option '{option_id}'."
        )


@dataclass(frozen=True)
class ConfirmationRequest:
    """Ask the user a yes/no question for a topic."""

    topic: str
    context: Mapping[str, object] = field(default_factory=dict)
    default: bool = False

    def __post_init__(self) -> None:
        _validate_topic(self.topic)


@dataclass(frozen=True)
class TextRequest:
    """Ask the user for free-form text for a topic."""

    topic: str
    context: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_topic(self.topic)


@dataclass(frozen=True)
class WorkflowNotice:
    """Fire-and-forget notice from a workflow to the user."""

    topic: str
    severity: Literal["info", "warning"] = "info"
    context: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_topic(self.topic)
        if self.severity not in ("info", "warning"):
            raise InteractionContractError(
                f"Notice '{self.topic}' has invalid severity '{self.severity}'."
            )


class InteractionPort(Protocol):
    """Everything a workflow module may ask of or tell the user."""

    def choose(self, request: ChoiceRequest) -> ChoiceOption: ...

    def confirm(self, request: ConfirmationRequest) -> bool: ...

    def request_text(self, request: TextRequest) -> str: ...

    def notify(self, notice: WorkflowNotice) -> None: ...


@dataclass(frozen=True)
class ScriptedResponse:
    """One planned answer for the scripted test adapter.

    ``kind`` is ``choose``, ``confirm``, or ``text``. For ``choose``, ``value``
    is the expected option_id, or None to take the request's default option.
    """

    kind: Literal["choose", "confirm", "text"]
    topic: str
    value: object = None


class ScriptedInteraction:
    """Test adapter: replays scripted responses in order and records requests.

    Notices are recorded but never consume script entries. Any decision request
    that does not match the next scripted response raises
    ScriptedInteractionError so tests fail on unexpected interaction flow.
    """

    def __init__(self, responses: Sequence[ScriptedResponse] = ()) -> None:
        self._responses = list(responses)
        self._cursor = 0
        self.requests: list[ChoiceRequest | ConfirmationRequest | TextRequest] = []
        self.notices: list[WorkflowNotice] = []

    @property
    def pending(self) -> int:
        return len(self._responses) - self._cursor

    def choose(self, request: ChoiceRequest) -> ChoiceOption:
        self.requests.append(request)
        scripted = self._next("choose", request.topic)
        if scripted.value is None:
            if request.default_option_id is None:
                raise ScriptedInteractionError(
                    f"Scripted default requested for '{request.topic}',"
                    " but the request has no default option."
                )
            return request.option(request.default_option_id)
        if not isinstance(scripted.value, str):
            raise ScriptedInteractionError(
                f"Scripted choose response for '{request.topic}' must be an option_id string."
            )
        return request.option(scripted.value)

    def confirm(self, request: ConfirmationRequest) -> bool:
        self.requests.append(request)
        scripted = self._next("confirm", request.topic)
        if not isinstance(scripted.value, bool):
            raise ScriptedInteractionError(
                f"Scripted confirm response for '{request.topic}' must be a bool."
            )
        return scripted.value

    def request_text(self, request: TextRequest) -> str:
        self.requests.append(request)
        scripted = self._next("text", request.topic)
        if not isinstance(scripted.value, str):
            raise ScriptedInteractionError(
                f"Scripted text response for '{request.topic}' must be a string."
            )
        return scripted.value

    def notify(self, notice: WorkflowNotice) -> None:
        self.notices.append(notice)

    def _next(self, kind: str, topic: str) -> ScriptedResponse:
        if self._cursor >= len(self._responses):
            raise ScriptedInteractionError(
                f"Unscripted interaction: {kind} '{topic}' (script exhausted)."
            )
        scripted = self._responses[self._cursor]
        if scripted.kind != kind or scripted.topic != topic:
            raise ScriptedInteractionError(
                f"Unexpected interaction: got {kind} '{topic}',"
                f" script expects {scripted.kind} '{scripted.topic}'."
            )
        self._cursor += 1
        return scripted


def _validate_topic(topic: str) -> None:
    if not topic or topic != topic.strip().lower():
        raise InteractionContractError(
            f"Interaction topic must be a non-empty lowercase identifier, got '{topic}'."
        )
