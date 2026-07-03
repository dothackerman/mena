"""Provider protocol validation for injected processing providers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mena.diarization import DiarizationProvider
from mena.semantic_coherence import SemanticCoherenceProvider
from mena.transcription import TranscriptionProvider

ProviderFactory = Callable[[], Any]


class ProviderValidationError(TypeError):
    """Raised when a configured processing provider does not satisfy its protocol."""


def require_provider(
    provider_factory: ProviderFactory,
    provider_type: type[Any],
    provider_name: str,
) -> Any:
    """Validate explicitly injected fake/test providers, not production runtime policy."""

    try:
        provider = provider_factory()
    except TypeError as exc:
        raise ProviderValidationError(
            f"{provider_name} provider must implement {provider_type.__name__}."
        ) from exc
    return require_provider_instance(provider, provider_type, provider_name)


def require_provider_instance(
    provider: Any,
    provider_type: type[Any],
    provider_name: str,
) -> Any:
    required: tuple[str, ...] = ()
    if provider_type is TranscriptionProvider:
        required = ("name", "transcribe")
    elif provider_type is DiarizationProvider:
        required = ("name", "config_id", "diarize")
    elif provider_type is SemanticCoherenceProvider:
        required = ("metadata", "check")

    if required:
        if not _provider_implements(provider, *required):
            raise ProviderValidationError(
                f"{provider_name} provider must implement {provider_type.__name__}."
            )
        if provider_type is SemanticCoherenceProvider:
            metadata = provider.metadata
            config_id = getattr(metadata, "config_id", None)
            if not isinstance(config_id, str) or not config_id:
                raise ProviderValidationError(
                    f"{provider_name} provider must expose semantic metadata config_id."
                )
            if not callable(getattr(metadata, "to_dict", None)):
                raise ProviderValidationError(
                    f"{provider_name} provider must expose semantic metadata to_dict."
                )
        return provider

    return provider


def _provider_implements(provider: Any, *required_attrs: str) -> bool:
    for attr in required_attrs:
        if not hasattr(provider, attr):
            return False
        if attr in {"transcribe", "diarize", "check"} and not callable(getattr(provider, attr)):
            return False
    return True
