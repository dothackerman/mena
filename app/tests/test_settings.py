from pathlib import Path

import pytest

from mena.settings import (
    DATA_ROOT_ENV_VAR,
    OLLAMA_ENDPOINT_ENV_VAR,
    OLLAMA_MODEL_ENV_VAR,
    SUMMARY_LANGUAGE_ENV_VAR,
    AppSettings,
    SettingsError,
    resolve_runtime_paths,
)


def test_resolve_runtime_paths_uses_xdg_defaults_under_home(tmp_path: Path) -> None:
    paths = resolve_runtime_paths(env={}, home=tmp_path)

    assert paths.data_home == tmp_path / ".local" / "share" / "mena"
    assert paths.sessions_dir == tmp_path / ".local" / "share" / "mena" / "sessions"
    assert paths.exports_dir == tmp_path / ".local" / "share" / "mena" / "exports"
    assert paths.config_home == tmp_path / ".config" / "mena"
    assert paths.cache_home == tmp_path / ".cache" / "mena"
    assert paths.model_cache_dir == tmp_path / ".cache" / "mena" / "models"
    assert paths.state_home == tmp_path / ".local" / "state" / "mena"
    assert paths.log_dir == tmp_path / ".local" / "state" / "mena" / "logs"


def test_resolve_runtime_paths_uses_xdg_environment(tmp_path: Path) -> None:
    env = {
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }

    paths = resolve_runtime_paths(env=env, home=tmp_path / "home")

    assert paths.data_home == tmp_path / "data" / "mena"
    assert paths.sessions_dir == tmp_path / "data" / "mena" / "sessions"
    assert paths.exports_dir == tmp_path / "data" / "mena" / "exports"
    assert paths.config_home == tmp_path / "config" / "mena"
    assert paths.cache_home == tmp_path / "cache" / "mena"
    assert paths.state_home == tmp_path / "state" / "mena"


def test_data_root_override_only_replaces_session_storage(tmp_path: Path) -> None:
    env = {
        DATA_ROOT_ENV_VAR: str(tmp_path / "sessions-root"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }

    paths = resolve_runtime_paths(env=env, home=tmp_path / "home")

    assert paths.data_home == tmp_path / "home" / ".local" / "share" / "mena"
    assert paths.sessions_dir == tmp_path / "sessions-root" / "sessions"
    assert paths.exports_dir == tmp_path / "home" / ".local" / "share" / "mena" / "exports"
    assert paths.config_home == tmp_path / "config" / "mena"
    assert paths.cache_home == tmp_path / "cache" / "mena"
    assert paths.model_cache_dir == tmp_path / "cache" / "mena" / "models"
    assert paths.state_home == tmp_path / "state" / "mena"
    assert paths.log_dir == tmp_path / "state" / "mena" / "logs"


def test_runtime_paths_keep_retention_categories_separate(tmp_path: Path) -> None:
    paths = resolve_runtime_paths(env={}, home=tmp_path)

    assert paths.sessions_dir.parent == paths.data_home
    assert paths.exports_dir.parent == paths.data_home
    assert paths.model_cache_dir.parent == paths.cache_home
    assert paths.log_dir.parent == paths.state_home
    assert paths.config_home != paths.cache_home
    assert paths.config_home != paths.state_home


def test_app_settings_wraps_resolved_paths(tmp_path: Path) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path)

    assert settings.paths.sessions_dir == tmp_path / ".local" / "share" / "mena" / "sessions"


def test_app_settings_uses_defaults_when_config_file_is_missing(tmp_path: Path) -> None:
    settings = AppSettings.from_env(env={}, home=tmp_path, config_path=tmp_path / "missing.toml")

    assert settings.summary.language == "en"
    assert settings.local_llm.ollama_endpoint == "http://localhost:11434"
    assert settings.local_llm.ollama_model == "llama3.2:3b"


def test_app_settings_rejects_broken_config_symlink(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.symlink_to(tmp_path / "missing-target.toml")

    with pytest.raises(SettingsError, match="Cannot read config file"):
        AppSettings.from_env(env={}, home=tmp_path, config_path=config_path)


def test_app_settings_wraps_config_path_inspection_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"

    def _raise_os_error(self: Path) -> object:
        if self == config_path:
            raise OSError("permission denied")
        return original_lstat(self)

    original_lstat = Path.lstat
    monkeypatch.setattr(Path, "lstat", _raise_os_error)

    with pytest.raises(SettingsError, match="Cannot inspect config file"):
        AppSettings.from_env(env={}, home=tmp_path, config_path=config_path)


def test_app_settings_loads_toml_config_values(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[summary]
language = "de-CH"

[ollama]
endpoint = "http://127.0.0.1:11435/"
model = "mistral:7b"
""".strip(),
        encoding="utf-8",
    )

    settings = AppSettings.from_env(env={}, home=tmp_path, config_path=config_path)

    assert settings.summary.language == "de-CH"
    assert settings.local_llm.ollama_endpoint == "http://127.0.0.1:11435"
    assert settings.local_llm.ollama_model == "mistral:7b"


def test_app_settings_environment_overrides_config_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[summary]
language = "de-CH"

[ollama]
endpoint = "http://localhost:11434"
model = "mistral:7b"
""".strip(),
        encoding="utf-8",
    )
    env = {
        SUMMARY_LANGUAGE_ENV_VAR: "fr-CH",
        OLLAMA_ENDPOINT_ENV_VAR: "http://127.0.0.1:11436",
        OLLAMA_MODEL_ENV_VAR: "llama3.1:8b",
    }

    settings = AppSettings.from_env(env=env, home=tmp_path, config_path=config_path)

    assert settings.summary.language == "fr-CH"
    assert settings.local_llm.ollama_endpoint == "http://127.0.0.1:11436"
    assert settings.local_llm.ollama_model == "llama3.1:8b"


def test_app_settings_rejects_unknown_config_keys(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[summary]\nstyle = 'brief'\n", encoding="utf-8")

    with pytest.raises(SettingsError, match="unknown key"):
        AppSettings.from_env(env={}, home=tmp_path, config_path=config_path)


def test_app_settings_rejects_wrong_config_types(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[summary]\nlanguage = 123\n", encoding="utf-8")

    with pytest.raises(SettingsError, match="must be a string"):
        AppSettings.from_env(env={}, home=tmp_path, config_path=config_path)


def test_app_settings_rejects_invalid_values(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[ollama]\nendpoint = 'https://api.example.com'\n", encoding="utf-8")

    with pytest.raises(SettingsError, match="must be local"):
        AppSettings.from_env(env={}, home=tmp_path, config_path=config_path)


def test_app_settings_rejects_invalid_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[summary\n", encoding="utf-8")

    with pytest.raises(SettingsError, match="invalid TOML"):
        AppSettings.from_env(env={}, home=tmp_path, config_path=config_path)
