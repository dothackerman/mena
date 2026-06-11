import importlib.metadata as metadata
import tomllib
from pathlib import Path

import mena

APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_ROOT.parent
PYPROJECT = APP_ROOT / "pyproject.toml"
README = REPO_ROOT / "README.md"


def test_package_has_version() -> None:
    assert mena.__version__ == "0.1.0rc3"


def test_console_script_entry_point_is_declared() -> None:
    entry_points = {
        entry.name: entry.value for entry in metadata.entry_points(group="console_scripts")
    }

    assert entry_points["mena"] == "mena.cli:main"


def test_package_metadata_keeps_core_dependencies_minimal() -> None:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]

    assert project["name"] == "mena"
    assert project["requires-python"] == ">=3.11"
    assert project["dependencies"] == []


def test_package_metadata_declares_optional_heavy_provider_extras() -> None:
    optional = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]

    assert "faster-whisper>=1.0" in optional["asr"]
    assert "pyannote.audio>=3.3" in optional["diarization"]
    assert "faster-whisper>=1.0" in optional["local-providers"]
    assert "pyannote.audio>=3.3" in optional["local-providers"]


def test_readme_documents_product_install_and_dev_commands() -> None:
    readme = README.read_text(encoding="utf-8")

    assert "uv build --wheel --out-dir dist" in readme
    assert 'MENA_WHEEL="$(ls -t dist/mena-*.whl | head -n 1)"' in readme
    assert 'uv tool install "${MENA_WHEEL}[local-providers]"' in readme
    assert "mena setup --check" in readme
    assert "mena setup --yes" in readme
    assert "uv sync --all-groups" in readme
    assert "uv run mena --version" in readme
    assert "Run setup preflight through the installed product command" in readme
    assert "make hooks" in readme
    assert "make check" in readme


def test_readme_documents_runtime_paths_and_safe_uninstall() -> None:
    readme = README.read_text(encoding="utf-8")

    assert "$XDG_DATA_HOME/mena/sessions" in readme
    assert "$XDG_CONFIG_HOME/mena/config.toml" in readme
    assert "$XDG_CACHE_HOME/mena/models" in readme
    assert "$XDG_STATE_HOME/mena/logs" in readme
    assert "must not delete sessions or exports" in readme
    assert "Uninstall is not cleanup" in readme
    assert "uv tool uninstall mena" in readme
