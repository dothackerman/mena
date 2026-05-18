"""Shared local LLM adapter helpers."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass

OLLAMA_DEFAULT_ENDPOINT = "http://localhost:11434"
OLLAMA_DEFAULT_MODEL = "llama3.2:3b"


class LocalLLMError(RuntimeError):
    """Base class for local LLM adapter failures."""


class LocalLLMContractError(LocalLLMError):
    """Raised when a local LLM request or response violates the adapter contract."""


class LocalLLMProviderUnavailable(LocalLLMError):
    """Raised when the configured local LLM provider cannot be reached."""


@dataclass(frozen=True)
class LocalLLMProviderMetadata:
    """Provider identity persisted with local LLM-derived artifacts."""

    name: str
    model: str
    endpoint: str | None
    prompt_policy: str

    @property
    def config_id(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "model": self.model,
            "endpoint": self.endpoint,
            "prompt_policy": self.prompt_policy,
        }


def local_ollama_endpoint(endpoint: str) -> str:
    """Return a normalized local Ollama endpoint or reject unsafe remote/secret-bearing URLs."""

    sanitized = endpoint.rstrip("/")
    parsed = urllib.parse.urlsplit(sanitized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LocalLLMContractError("Ollama endpoint must be an HTTP URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LocalLLMContractError(
            "Ollama endpoint must not contain credentials, query, or fragment."
        )
    hostname = parsed.hostname
    if hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise LocalLLMContractError("Ollama endpoint must be local.")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def post_ollama_json(
    *,
    endpoint: str,
    path: str,
    payload: Mapping[str, object],
    timeout_seconds: float,
) -> Mapping[str, object]:
    """Post JSON to a local Ollama endpoint and return a JSON object response envelope."""

    url = f"{endpoint}{path}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except OSError as exc:
        raise LocalLLMProviderUnavailable("Ollama provider is unavailable.") from exc
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LocalLLMContractError("Ollama response envelope was not JSON.") from exc
    return mapping(decoded, "Ollama response envelope")


def ollama_message_content(response: Mapping[str, object]) -> str:
    """Extract assistant message content from an Ollama chat response envelope."""

    message = response.get("message")
    if not isinstance(message, Mapping):
        raise LocalLLMContractError("Ollama response missing message object.")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise LocalLLMContractError("Ollama response message content is empty.")
    return content


def strip_json_fence(content: str) -> str:
    """Remove common markdown JSON fences from model output."""

    stripped = content.strip()
    if stripped.startswith("```json") and stripped.endswith("```"):
        return stripped.removeprefix("```json").removesuffix("```").strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        return stripped.removeprefix("```").removesuffix("```").strip()
    return stripped


def mapping(data: object, label: str) -> Mapping[str, object]:
    """Return a JSON object mapping or raise a local LLM contract error."""

    if not isinstance(data, Mapping):
        raise LocalLLMContractError(f"{label} must be a JSON object.")
    return data
