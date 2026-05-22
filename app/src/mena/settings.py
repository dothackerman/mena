"""Runtime path and settings foundation for the mena PoC."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DATA_ROOT_ENV_VAR = "MENA_DATA_ROOT"


@dataclass(frozen=True)
class RuntimePaths:
    """Resolved local runtime paths for mena."""

    data_home: Path
    sessions_dir: Path
    exports_dir: Path
    config_home: Path
    cache_home: Path
    model_cache_dir: Path
    state_home: Path
    log_dir: Path


@dataclass(frozen=True)
class AppSettings:
    """Typed application settings with safe defaults."""

    paths: RuntimePaths

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        home: Path | None = None,
    ) -> AppSettings:
        """Build settings from environment variables without reading config files."""

        return cls(paths=resolve_runtime_paths(env, home=home))


def resolve_runtime_paths(
    env: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> RuntimePaths:
    """Resolve XDG-style runtime paths.

    `MENA_DATA_ROOT` is retained as the narrow session-data override used by tests and smoke runs.
    """

    current_env = os.environ if env is None else env
    current_home = home or Path.home()

    data_home = _mena_root(
        current_env,
        env_key="XDG_DATA_HOME",
        default_base=current_home / ".local" / "share",
    )
    sessions_root = data_home
    if configured_data_root := current_env.get(DATA_ROOT_ENV_VAR):
        sessions_root = Path(configured_data_root).expanduser()

    config_home = _mena_root(
        current_env,
        env_key="XDG_CONFIG_HOME",
        default_base=current_home / ".config",
    )
    cache_home = _mena_root(
        current_env,
        env_key="XDG_CACHE_HOME",
        default_base=current_home / ".cache",
    )
    state_home = _mena_root(
        current_env,
        env_key="XDG_STATE_HOME",
        default_base=current_home / ".local" / "state",
    )

    return RuntimePaths(
        data_home=data_home,
        sessions_dir=sessions_root / "sessions",
        exports_dir=data_home / "exports",
        config_home=config_home,
        cache_home=cache_home,
        model_cache_dir=cache_home / "models",
        state_home=state_home,
        log_dir=state_home / "logs",
    )


def _mena_root(env: Mapping[str, str], *, env_key: str, default_base: Path) -> Path:
    configured_base = env.get(env_key)
    base = Path(configured_base).expanduser() if configured_base else default_base
    return base / "mena"
