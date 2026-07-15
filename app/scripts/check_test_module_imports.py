"""Fail when one test module imports another test module."""

import ast
from pathlib import Path

LEGACY_IMPORTS = {
    ("test_cli.py", "test_correction_workflow"),
    ("test_mistral_summarization.py", "test_mistral_llm"),
    ("test_mistral_summarization.py", "test_summarization"),
    ("test_note_package.py", "test_summarization"),
    ("test_recording_control.py", "test_cli"),
}


def cross_test_imports(path: Path) -> list[tuple[int, str]]:
    violations: list[tuple[int, str]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_test_module(alias.name):
                    violations.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _is_test_module(module):
                violations.append((node.lineno, module))
            elif node.level > 0:
                for alias in node.names:
                    if _is_test_module(alias.name):
                        violations.append((node.lineno, alias.name))
    return violations


def _is_test_module(module: str) -> bool:
    return any(part.startswith("test_") for part in module.split("."))


def main() -> int:
    tests_root = Path(__file__).parents[1] / "tests"
    failures: list[str] = []
    for path in sorted(tests_root.rglob("test_*.py")):
        for line, imported_module in cross_test_imports(path):
            relative_path = path.relative_to(tests_root).as_posix()
            if (relative_path, imported_module) in LEGACY_IMPORTS:
                continue
            failures.append(
                f"{path.relative_to(tests_root.parent)}:{line}: imports {imported_module}"
            )
    if failures:
        print("Test-module import boundary violated (use tests/support arrangement modules):")
        for failure in failures:
            print(f"  {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
