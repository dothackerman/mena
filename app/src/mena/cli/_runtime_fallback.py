"""Runtime Fallback for the CLI adapter."""

from __future__ import annotations

from mena.runtime_stewardship import (
    RuntimeReleaseResult,
)


def _print_runtime_stewardship_release_notice(result: RuntimeReleaseResult) -> None:
    if result.status in {"failed", "not_supported"}:
        print(f"Runtime stewardship warning: {result.detail}")
    else:
        print(f"Runtime stewardship: {result.detail}")
