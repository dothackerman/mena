from pathlib import Path

from mena.settings import DATA_ROOT_ENV_VAR, AppSettings, resolve_runtime_paths


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
