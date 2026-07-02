"""Shared file-system helpers for mutable JSON records."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path


def write_json_atomically(path: Path, data: Mapping[str, object]) -> None:
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)
