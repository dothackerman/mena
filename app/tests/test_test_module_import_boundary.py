from collections.abc import Callable
from pathlib import Path
from runpy import run_path
from typing import cast

import pytest

CrossTestImports = Callable[[Path], list[tuple[int, str]]]

cross_test_imports = cast(
    CrossTestImports,
    run_path(str(Path(__file__).parents[1] / "scripts/check_test_module_imports.py"))[
        "cross_test_imports"
    ],
)


@pytest.mark.parametrize(
    ("source", "imported_module"),
    [
        ("from tests import test_cli\n", "test_cli"),
        ("from . import test_cli\n", "test_cli"),
        ("from test_cli import helper\n", "test_cli"),
        ("import tests.test_cli\n", "tests.test_cli"),
    ],
)
def test_cross_test_imports_rejects_equivalent_import_spellings(
    tmp_path: Path,
    source: str,
    imported_module: str,
) -> None:
    test_module = tmp_path / "test_consumer.py"
    test_module.write_text(source, encoding="utf-8")

    assert cross_test_imports(test_module) == [(1, imported_module)]


def test_cross_test_imports_allows_test_support_import(tmp_path: Path) -> None:
    test_module = tmp_path / "test_consumer.py"
    test_module.write_text(
        "from tests.support.scenarios import summarization\n",
        encoding="utf-8",
    )

    assert cross_test_imports(test_module) == []
