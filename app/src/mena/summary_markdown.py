"""Deterministic markdown rendering for structured summary artifacts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol

from mena.summarization import SummaryActionItem, SummaryArtifact, SummaryResult

SummaryMarkdownSource = SummaryArtifact | SummaryResult | Mapping[str, object]


class _ActionItemPayload(Protocol):
    text: str
    owner: str | None
    due: str | None


def format_summary_markdown(summary: SummaryMarkdownSource) -> str:
    """Render a validated summary payload as stable markdown meeting notes."""

    payload = _validated_payload(summary)
    sections = (
        ("Timeline", _bullet_list(payload.timeline)),
        ("Important points", _bullet_list(payload.important_points)),
        (
            "Action items",
            _bullet_list(_format_action_item(action) for action in payload.action_items),
        ),
        ("General summary", payload.general_summary),
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


def _format_action_item(action: SummaryActionItem | _ActionItemPayload) -> str:
    text = action.text
    details = []
    owner = action.owner
    due = action.due
    if owner is not None:
        details.append(f"Owner: {owner}")
    if due is not None:
        details.append(f"Due: {due}")
    if not details:
        return text
    return f"{text} ({'; '.join(details)})"


def _indent_continuation(text: str) -> str:
    return text.replace("\n", "\n  ")
