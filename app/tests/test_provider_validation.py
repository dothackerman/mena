from types import SimpleNamespace

import pytest

from mena.diarization import DiarizationProvider
from mena.provider_validation import require_provider
from mena.semantic_coherence import SemanticCoherenceProvider
from mena.summarization import SummarizationProvider
from mena.transcription import TranscriptionProvider


def test_require_provider_type_requirements() -> None:
    with pytest.raises(
        TypeError, match="transcription provider must implement TranscriptionProvider"
    ):
        require_provider(
            provider_factory=lambda: SimpleNamespace(name="bad"),
            provider_type=TranscriptionProvider,
            provider_name="transcription",
        )

    with pytest.raises(TypeError, match="bogus provider must implement int"):
        require_provider(
            provider_factory=lambda: "nope",
            provider_type=int,
            provider_name="bogus",
        )

    with pytest.raises(
        TypeError, match="semantic_coherence provider must expose semantic metadata config_id"
    ):
        require_provider(
            provider_factory=lambda: SimpleNamespace(metadata={}, check=lambda *_args: None),
            provider_type=SemanticCoherenceProvider,
            provider_name="semantic_coherence",
        )

    with pytest.raises(
        TypeError, match="semantic_coherence provider must expose semantic metadata to_dict"
    ):
        require_provider(
            provider_factory=lambda: SimpleNamespace(
                metadata=SimpleNamespace(config_id="cfg"),
                check=lambda *_args: None,
            ),
            provider_type=SemanticCoherenceProvider,
            provider_name="semantic_coherence",
        )

    with pytest.raises(
        TypeError, match="summarization provider must expose summarization metadata config_id"
    ):
        require_provider(
            provider_factory=lambda: SimpleNamespace(metadata={}, summarize=lambda *_args: None),
            provider_type=SummarizationProvider,
            provider_name="summarization",
        )

    with pytest.raises(
        TypeError, match="summarization provider must expose summarization metadata to_dict"
    ):
        require_provider(
            provider_factory=lambda: SimpleNamespace(
                metadata=SimpleNamespace(config_id="cfg"),
                summarize=lambda *_args: None,
            ),
            provider_type=SummarizationProvider,
            provider_name="summarization",
        )

    with pytest.raises(
        TypeError, match="transcription provider must implement TranscriptionProvider"
    ):
        require_provider(
            provider_factory=lambda: SimpleNamespace(name="bad", transcribe="not-callable"),
            provider_type=TranscriptionProvider,
            provider_name="transcription",
        )

    with pytest.raises(TypeError, match="diarization provider must implement DiarizationProvider"):
        require_provider(
            provider_factory=lambda: SimpleNamespace(
                name="bad",
                config_id="cfg",
                diarize=None,
            ),
            provider_type=DiarizationProvider,
            provider_name="diarization",
        )
