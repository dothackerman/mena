"""Static import-boundary checks used by repository validation."""

from __future__ import annotations

import ast
from pathlib import Path


def cli_imports(path: Path) -> list[tuple[int, str]]:
    violations: list[tuple[int, str]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "mena.cli" or alias.name.startswith("mena.cli."):
                    violations.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "mena.cli" or module.startswith("mena.cli."):
                violations.append((node.lineno, module))
            elif module == "mena" and any(alias.name == "cli" for alias in node.names):
                violations.append((node.lineno, "mena.cli"))
            elif node.level > 0 and (module == "cli" or module.startswith("cli.")):
                violations.append((node.lineno, f"mena.{module}"))
            elif node.level > 0 and not module and any(alias.name == "cli" for alias in node.names):
                violations.append((node.lineno, "mena.cli"))
    return violations
