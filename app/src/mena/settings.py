"""Runtime path, configuration, and settings foundation for the mena PoC."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mena.local_llm import (
    OLLAMA_DEFAULT_ENDPOINT,
    OLLAMA_DEFAULT_MODEL,
    LocalLLMContractError,
    local_ollama_endpoint,
)

DATA_ROOT_ENV_VAR = "MENA_DATA_ROOT"
CONFIG_FILENAME = "config.toml"
SUMMARY_LANGUAGE_ENV_VAR = "MENA_SUMMARY_LANGUAGE"
OLLAMA_ENDPOINT_ENV_VAR = "MENA_OLLAMA_ENDPOINT"
OLLAMA_MODEL_ENV_VAR = "MENA_OLLAMA_MODEL"
DEFAULT_SUMMARY_LANGUAGE = "en"
_ALLOWED_TOP_LEVEL_CONFIG_KEYS = frozenset({"summary", "ollama"})
_ALLOWED_SUMMARY_CONFIG_KEYS = frozenset({"language"})
_ALLOWED_OLLAMA_CONFIG_KEYS = frozenset({"endpoint", "model"})
_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(?:-[A-Za-z0-9_-]+)*$")


class SettingsError(ValueError):
    """Raised when configuration cannot be loaded or validated."""


def validate_summary_language(value: object, label: str) -> str:
    """Validate and return a configured summary language code."""

    return normalize_summary_language(value, label)


def normalize_summary_language(value: object, label: str) -> str:
    """Validate and return the canonical summary language code."""

    language = _language(value, label).replace("_", "-")
    primary, separator, rest = language.partition("-")
    return f"{primary.lower()}{separator}{rest}"


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
class SummarySettings:
    """User-configurable summarization defaults."""

    language: str = DEFAULT_SUMMARY_LANGUAGE


@dataclass(frozen=True)
class LocalLLMSettings:
    """User-configurable local LLM defaults."""

    ollama_endpoint: str = OLLAMA_DEFAULT_ENDPOINT
    ollama_model: str = OLLAMA_DEFAULT_MODEL


@dataclass(frozen=True)
class AppSettings:
    """Typed application settings with deterministic precedence."""

    paths: RuntimePaths
    summary: SummarySettings = field(default_factory=SummarySettings)
    local_llm: LocalLLMSettings = field(default_factory=LocalLLMSettings)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        home: Path | None = None,
        config_path: Path | None = None,
    ) -> AppSettings:
        """Build settings from defaults, optional TOML config, and environment overrides."""

        current_env = os.environ if env is None else env
        paths = resolve_runtime_paths(current_env, home=home)
        resolved_config_path = config_path or paths.config_home / CONFIG_FILENAME
        return _resolve_app_settings(
            paths=paths,
            env=current_env,
            config_path=resolved_config_path,
        )


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


def _resolve_app_settings(
    *,
    paths: RuntimePaths,
    env: Mapping[str, str],
    config_path: Path,
) -> AppSettings:
    config = _load_config_file(config_path)
    summary = SummarySettings()
    local_llm = LocalLLMSettings(ollama_endpoint=local_ollama_endpoint(OLLAMA_DEFAULT_ENDPOINT))
    summary, local_llm = _apply_config(config, summary=summary, local_llm=local_llm)
    summary, local_llm = _apply_env(env, summary=summary, local_llm=local_llm)
    return AppSettings(paths=paths, summary=summary, local_llm=local_llm)


def _load_config_file(config_path: Path) -> Mapping[str, Any]:
    try:
        config_path.lstat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise SettingsError(f"Cannot inspect config file {config_path}: {exc}") from exc

    try:
        with config_path.open("rb") as config_file:
            data = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"Config file {config_path} is invalid TOML: {exc}") from exc
    except OSError as exc:
        raise SettingsError(f"Cannot read config file {config_path}: {exc}") from exc
    return _mapping(data, "Config file")


def _apply_config(
    config: Mapping[str, Any],
    *,
    summary: SummarySettings,
    local_llm: LocalLLMSettings,
) -> tuple[SummarySettings, LocalLLMSettings]:
    _reject_unknown_keys(config, _ALLOWED_TOP_LEVEL_CONFIG_KEYS, "Config file")

    summary_section = _optional_section(config, "summary")
    if summary_section is not None:
        _reject_unknown_keys(summary_section, _ALLOWED_SUMMARY_CONFIG_KEYS, "[summary]")
        if "language" in summary_section:
            summary = SummarySettings(
                language=validate_summary_language(
                    summary_section["language"], "[summary].language"
                )
            )

    ollama_section = _optional_section(config, "ollama")
    if ollama_section is not None:
        _reject_unknown_keys(ollama_section, _ALLOWED_OLLAMA_CONFIG_KEYS, "[ollama]")
        endpoint = local_llm.ollama_endpoint
        model = local_llm.ollama_model
        if "endpoint" in ollama_section:
            endpoint = _ollama_endpoint(ollama_section["endpoint"], "[ollama].endpoint")
        if "model" in ollama_section:
            model = _model_name(ollama_section["model"], "[ollama].model")
        local_llm = LocalLLMSettings(ollama_endpoint=endpoint, ollama_model=model)

    return summary, local_llm


def _apply_env(
    env: Mapping[str, str],
    *,
    summary: SummarySettings,
    local_llm: LocalLLMSettings,
) -> tuple[SummarySettings, LocalLLMSettings]:
    if SUMMARY_LANGUAGE_ENV_VAR in env:
        summary = SummarySettings(
            language=validate_summary_language(
                env[SUMMARY_LANGUAGE_ENV_VAR], SUMMARY_LANGUAGE_ENV_VAR
            )
        )

    endpoint = local_llm.ollama_endpoint
    model = local_llm.ollama_model
    if OLLAMA_ENDPOINT_ENV_VAR in env:
        endpoint = _ollama_endpoint(env[OLLAMA_ENDPOINT_ENV_VAR], OLLAMA_ENDPOINT_ENV_VAR)
    if OLLAMA_MODEL_ENV_VAR in env:
        model = _model_name(env[OLLAMA_MODEL_ENV_VAR], OLLAMA_MODEL_ENV_VAR)
    local_llm = LocalLLMSettings(ollama_endpoint=endpoint, ollama_model=model)

    return summary, local_llm


def _optional_section(config: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    if key not in config:
        return None
    value = config[key]
    if not isinstance(value, Mapping):
        raise SettingsError(f"Config section [{key}] must be a table.")
    return value


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SettingsError(f"{label} must be a TOML table.")
    return value


def _reject_unknown_keys(value: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        joined = ", ".join(unknown)
        raise SettingsError(f"{label} contains unknown key(s): {joined}.")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise SettingsError(f"{label} must be a string.")
    stripped = value.strip()
    if stripped != value or not stripped:
        raise SettingsError(f"{label} must be a non-empty string without surrounding whitespace.")
    return value


def _language(value: object, label: str) -> str:
    language = _string(value, label)
    if not _LANGUAGE_PATTERN.fullmatch(language):
        raise SettingsError(f"{label} must be a language code like 'en' or 'de-CH'.")
    return language


def _model_name(value: object, label: str) -> str:
    return _string(value, label)


def _ollama_endpoint(value: object, label: str) -> str:
    endpoint = _string(value, label)
    try:
        return local_ollama_endpoint(endpoint)
    except LocalLLMContractError as exc:
        raise SettingsError(f"{label} is invalid: {exc}") from exc


def _mena_root(env: Mapping[str, str], *, env_key: str, default_base: Path) -> Path:
    configured_base = env.get(env_key)
    base = Path(configured_base).expanduser() if configured_base else default_base
    return base / "mena"
