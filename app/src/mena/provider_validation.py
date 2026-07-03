"""Provider protocol validation for injected processing providers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mena.diarization import DiarizationProvider
from mena.semantic_coherence import SemanticCoherenceProvider
from mena.summarization import SummarizationProvider
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
    if provider_type in {
        TranscriptionProvider,
        DiarizationProvider,
        SemanticCoherenceProvider,
        SummarizationProvider,
    }:
        if not isinstance(provider, provider_type) or not _protocol_methods_callable(
            provider, provider_type
        ):
            raise ProviderValidationError(
                f"{provider_name} provider must implement {provider_type.__name__}."
            )
        if provider_type is SemanticCoherenceProvider:
            _require_provider_metadata(provider, provider_name, "semantic")
        if provider_type is SummarizationProvider:
            _require_provider_metadata(provider, provider_name, "summarization")
        return provider

    try:
        valid = isinstance(provider, provider_type)
    except TypeError:
        valid = False
    if not valid:
        raise ProviderValidationError(
            f"{provider_name} provider must implement {provider_type.__name__}."
        )
    return provider


def _protocol_methods_callable(provider: Any, provider_type: type[Any]) -> bool:
    method_names = (
        name
        for name, member in vars(provider_type).items()
        if not name.startswith("_") and callable(member)
    )
    return all(callable(getattr(provider, method_name, None)) for method_name in method_names)


def _require_provider_metadata(provider: Any, provider_name: str, metadata_name: str) -> None:
    metadata = provider.metadata
    config_id = getattr(metadata, "config_id", None)
    if not isinstance(config_id, str) or not config_id:
        raise ProviderValidationError(
            f"{provider_name} provider must expose {metadata_name} metadata config_id."
        )
    if not callable(getattr(metadata, "to_dict", None)):
        raise ProviderValidationError(
            f"{provider_name} provider must expose {metadata_name} metadata to_dict."
        )
