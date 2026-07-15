"""Fail when application/core modules import the CLI interface adapter."""

from __future__ import annotations

from pathlib import Path

from mena.import_boundaries import cli_imports


def main() -> int:
    source_root = Path(__file__).parents[1] / "src" / "mena"
    failures: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        if path.is_relative_to(source_root / "cli") or path == source_root / "__main__.py":
            continue
        for line, imported_module in cli_imports(path):
            failures.append(
                f"{path.relative_to(source_root.parent)}:{line}: imports {imported_module}"
            )
    if failures:
        print("CLI import boundary violated (core modules must not import the interface adapter):")
        for failure in failures:
            print(f"  {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
