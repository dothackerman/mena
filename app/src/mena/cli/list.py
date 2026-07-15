"""``mena list`` parser and rendering handler."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from mena.cli.command import bind_handler
from mena.cli.composition import CommandContext
from mena.session_integrity import (
    SessionIndexEntry,
    rebuild_session_index,
)
from mena.timestamps import parse_utc_timestamp

UNTITLED_SESSION_NAME = "Untitled recording"


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser], **_: object) -> None:
    parser = subparsers.add_parser("list", help="List discovered session bundles.")
    bind_handler(parser, run)


def run(args: argparse.Namespace, context: CommandContext) -> int:
    return _run_list_command(sessions_root=context.sessions_root)


def _run_list_command(*, sessions_root: Path) -> int:
    """Render the rebuildable global-index projection as one line per session.

    The workflow interface is the existing index projection
    (``rebuild_session_index``); this command is a rendering adapter over it and
    never persists the index to disk (ADR-018). Sorting is a rendering concern:
    the projection returns entries in bundle-path order, so newest-first ordering
    is applied here.
    """

    entries = rebuild_session_index(sessions_root)
    if not entries:
        print("No sessions found.")
        return 0
    for entry in _sort_session_index_newest_first(entries):
        print(_format_session_index_line(entry))
    return 0


def _sort_session_index_newest_first(
    entries: Sequence[SessionIndexEntry],
) -> list[SessionIndexEntry]:
    parseable: list[tuple[datetime, SessionIndexEntry]] = []
    unparseable: list[SessionIndexEntry] = []
    for entry in entries:
        parsed = _parse_iso_timestamp(entry.created_at)
        if parsed is None:
            unparseable.append(entry)
        else:
            parseable.append((parsed, entry))
    # Stable sort keyed only on the timestamp keeps entries with equal (or
    # unparseable) timestamps in the projection's original order.
    parseable.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in parseable] + unparseable


def _format_session_index_line(entry: SessionIndexEntry) -> str:
    fields = (
        entry.created_at,
        entry.session_id[:8],
        entry.name or UNTITLED_SESSION_NAME,
        entry.profile,
        entry.status,
        str(entry.bundle_path),
    )
    line = " | ".join(fields)
    if not entry.integrity_ok:
        reason_suffix = ""
        if entry.finding_reasons:
            reason_suffix = ": " + "; ".join(entry.finding_reasons)
        line = f"{line} | DEGRADED ({entry.finding_count} integrity finding(s){reason_suffix})"
    return line


def _parse_iso_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return parse_utc_timestamp(value)
    except ValueError:
        return None
