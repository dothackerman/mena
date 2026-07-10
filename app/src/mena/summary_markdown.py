"""Deterministic markdown rendering for structured summary artifacts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol

from mena.settings import normalize_summary_language
from mena.summarization import (
    DEFAULT_LANGUAGE,
    SummaryActionItem,
    SummaryArtifact,
    SummaryResult,
    SummaryTimelineEntry,
)

SummaryMarkdownSource = SummaryArtifact | SummaryResult | Mapping[str, object]


class _ActionItemPayload(Protocol):
    text: str
    owner: str | None
    due: str | None


def format_summary_markdown(summary: SummaryMarkdownSource) -> str:
    """Render a validated summary payload as stable markdown meeting notes."""

    payload = _validated_payload(summary)
    labels = _labels_for_language(_payload_language(payload))
    sections = (
        (
            labels.timeline,
            _bullet_list(_format_timeline_entry(entry) for entry in payload.timeline),
        ),
        (
            labels.action_items,
            _bullet_list(
                _format_action_item(action, labels=labels) for action in payload.action_items
            ),
        ),
        (labels.general_summary, payload.general_summary),
    )
    return "\n\n".join(_section(title=title, body=body) for title, body in sections) + "\n"


def _validated_payload(summary: SummaryMarkdownSource) -> SummaryArtifact | SummaryResult:
    if isinstance(summary, SummaryArtifact):
        return SummaryArtifact.from_dict(summary.to_dict())
    if isinstance(summary, SummaryResult):
        return SummaryResult.from_dict(summary.to_dict())
    if "version" in summary:
        return SummaryArtifact.from_dict(summary)
    return SummaryResult.from_dict(summary)


def _section(*, title: str, body: str) -> str:
    return f"## {title}\n\n{body}"


def _bullet_list(items: Iterable[object]) -> str:
    return "\n".join(f"- {_indent_continuation(str(item))}" for item in items)


def _format_action_item(action: SummaryActionItem | _ActionItemPayload, *, labels: _Labels) -> str:
    text = action.text
    details = []
    owner = action.owner
    due = action.due
    if owner is not None:
        details.append(f"{labels.owner}: {owner}")
    if due is not None:
        details.append(f"{labels.due}: {due}")
    if not details:
        return text
    return f"{text} ({'; '.join(details)})"


def _format_timeline_entry(entry: SummaryTimelineEntry) -> str:
    return f"[{_format_seconds(entry.start_seconds)}] {entry.text}"


def _format_seconds(value: float) -> str:
    total_seconds = int(value)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _indent_continuation(text: str) -> str:
    return text.replace("\n", "\n  ")


class _Labels(Protocol):
    timeline: str
    action_items: str
    general_summary: str
    owner: str
    due: str


class _EnglishLabels:
    timeline = "Timeline"
    action_items = "Action items"
    general_summary = "General summary"
    owner = "Owner"
    due = "Due"


class _GermanLabels:
    timeline = "Zeitverlauf"
    action_items = "Aufgaben"
    general_summary = "Zusammenfassung"
    owner = "Verantwortlich"
    due = "Fällig"


def _payload_language(payload: SummaryArtifact | SummaryResult) -> str:
    if isinstance(payload, SummaryArtifact):
        language = payload.policy.get("language")
        if isinstance(language, str):
            return language
    return DEFAULT_LANGUAGE


def _labels_for_language(language: str) -> _Labels:
    normalized = normalize_summary_language(language, "summary language")
    if normalized.split("-", maxsplit=1)[0] == "de":
        return _GermanLabels()
    return _EnglishLabels()
