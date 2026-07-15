from pathlib import Path

from mena.import_boundaries import cli_imports


def test_cli_import_boundary_detects_direct_and_nested_imports(tmp_path: Path) -> None:
    violating = tmp_path / "workflow.py"
    violating.write_text(
        "import mena.cli\n"
        "from mena.cli.record import run\n"
        "from mena import cli\n"
        "from .cli import main\n"
        "from . import cli\n"
        "from mena import settings\n",
        encoding="utf-8",
    )

    assert cli_imports(violating) == [
        (1, "mena.cli"),
        (2, "mena.cli.record"),
        (3, "mena.cli"),
        (4, "mena.cli"),
        (5, "mena.cli"),
    ]


def test_cli_import_boundary_accepts_core_imports(tmp_path: Path) -> None:
    valid = tmp_path / "workflow.py"
    valid.write_text("from mena import settings\n", encoding="utf-8")

    assert cli_imports(valid) == []
