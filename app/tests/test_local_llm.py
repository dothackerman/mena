from __future__ import annotations

import json
from typing import Any

import pytest

from mena.local_llm import LocalLLMContractError, unload_ollama_model


def test_unload_ollama_model_posts_empty_chat_with_zero_keep_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"done": true, "done_reason": "unload"}'

    def fake_urlopen(request: Any, *, timeout: float) -> FakeResponse:
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("mena.local_llm.urllib.request.urlopen", fake_urlopen)

    result = unload_ollama_model(
        endpoint="http://localhost:11434",
        model="llama3.2:3b",
        timeout_seconds=12.0,
    )

    assert result == "unload"
    assert captured == {
        "url": "http://localhost:11434/api/chat",
        "timeout": 12.0,
        "payload": {
            "model": "llama3.2:3b",
            "messages": [],
            "keep_alive": 0,
        },
    }


def test_unload_ollama_model_rejects_incomplete_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"done": false}'

    monkeypatch.setattr(
        "mena.local_llm.urllib.request.urlopen",
        lambda request, *, timeout: FakeResponse(),
    )

    with pytest.raises(LocalLLMContractError, match="did not complete"):
        unload_ollama_model(
            endpoint="http://localhost:11434",
            model="llama3.2:3b",
            timeout_seconds=12.0,
        )
