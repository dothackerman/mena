"""Shared UTC timestamp formatting helpers."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    return current.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_timestamp_for_filename(value: datetime | None = None) -> str:
    return utc_timestamp(value).replace(":", "")
