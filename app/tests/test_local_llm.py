from __future__ import annotations

import json
import urllib.error
from typing import Any

import pytest

from mena.local_llm import (
    LocalLLMContractError,
    LocalLLMProviderUnavailable,
    LocalLLMTimeout,
    ollama_token_counts,
    post_ollama_json,
    unload_ollama_model,
)


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("timed out"), urllib.error.URLError(TimeoutError("timed out"))],
)
def test_post_ollama_json_distinguishes_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    monkeypatch.setattr(
        "mena.local_llm.urllib.request.urlopen",
        lambda _request, *, timeout: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(LocalLLMTimeout, match="request timed out"):
        post_ollama_json(
            endpoint="http://localhost:11434",
            path="/api/chat",
            payload={},
            timeout_seconds=600.0,
        )


def test_post_ollama_json_keeps_connection_failure_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "mena.local_llm.urllib.request.urlopen",
        lambda _request, *, timeout: (_ for _ in ()).throw(ConnectionRefusedError()),
    )

    with pytest.raises(LocalLLMProviderUnavailable, match="provider is unavailable"):
        post_ollama_json(
            endpoint="http://localhost:11434",
            path="/api/chat",
            payload={},
            timeout_seconds=600.0,
        )

    monkeypatch.setattr(
        "mena.local_llm.urllib.request.urlopen",
        lambda _request, *, timeout: (_ for _ in ()).throw(
            urllib.error.URLError("connection refused")
        ),
    )
    with pytest.raises(LocalLLMProviderUnavailable, match="provider is unavailable"):
        post_ollama_json(
            endpoint="http://localhost:11434",
            path="/api/chat",
            payload={},
            timeout_seconds=600.0,
        )


def test_post_ollama_json_distinguishes_timeout_while_reading_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimeoutResponse:
        def __enter__(self) -> TimeoutResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            raise TimeoutError("timed out while reading")

    monkeypatch.setattr(
        "mena.local_llm.urllib.request.urlopen",
        lambda _request, *, timeout: TimeoutResponse(),
    )

    with pytest.raises(LocalLLMTimeout, match="request timed out"):
        post_ollama_json(
            endpoint="http://localhost:11434",
            path="/api/chat",
            payload={},
            timeout_seconds=600.0,
        )


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


def test_ollama_token_counts_reject_negative_counts() -> None:
    with pytest.raises(LocalLLMContractError, match="non-negative"):
        ollama_token_counts({"prompt_eval_count": -1, "eval_count": 0})

    with pytest.raises(LocalLLMContractError, match="non-negative"):
        ollama_token_counts({"prompt_eval_count": 0, "eval_count": -1})


@pytest.mark.parametrize(
    ("response_payload", "message"),
    [
        (b'{"done": false}', "did not complete"),
        (b'{"done": true}', "done_reason is invalid"),
        (b'{"done": true, "done_reason": "load"}', "did not report done_reason=unload"),
    ],
)
def test_unload_ollama_model_rejects_non_unload_responses(
    monkeypatch: pytest.MonkeyPatch,
    response_payload: bytes,
    message: str,
) -> None:
    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return response_payload

    monkeypatch.setattr(
        "mena.local_llm.urllib.request.urlopen",
        lambda request, *, timeout: FakeResponse(),
    )

    with pytest.raises(LocalLLMContractError, match=message):
        unload_ollama_model(
            endpoint="http://localhost:11434",
            model="llama3.2:3b",
            timeout_seconds=12.0,
        )
