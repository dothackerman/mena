"""Local Ollama adapter for structured meeting note-package summarization."""

from __future__ import annotations

import json
from collections.abc import Mapping

from mena.local_llm import (
    OLLAMA_DEFAULT_ENDPOINT,
    OLLAMA_DEFAULT_MODEL,
    LocalLLMContractError,
    LocalLLMProviderUnavailable,
    local_ollama_endpoint,
    ollama_message_content,
    ollama_token_counts,
    post_ollama_json,
    strip_json_fence,
    unload_ollama_model,
)
from mena.note_package import (
    ACTION_ITEMS_RESPONSE_SCHEMA,
    GENERAL_SUMMARY_RESPONSE_SCHEMA,
    TIMELINE_RESPONSE_SCHEMA,
    ProductRequestRecord,
    SummarizationContractError,
    SummarizationRequest,
    SummaryResult,
    assemble_summary_result,
    assert_summary_output_language,
    guard_summarization_token_budget,
    product_system_prompt,
    product_user_prompt,
    validate_product_payload,
)
from mena.runtime_stewardship import RuntimeLease, RuntimeReleaseResult
from mena.summarization import SummarizationProviderMetadata, SummarizationProviderUnavailable

OLLAMA_SUMMARIZATION_NUM_CTX = 8192
OLLAMA_SUMMARIZATION_SEED = 0
OLLAMA_SUMMARIZATION_TEMPERATURE = 0
OLLAMA_DETERMINISTIC_OPTIONS: dict[str, object] = {
    "temperature": OLLAMA_SUMMARIZATION_TEMPERATURE,
    "seed": OLLAMA_SUMMARIZATION_SEED,
    "num_ctx": OLLAMA_SUMMARIZATION_NUM_CTX,
}


class OllamaSummarizationProvider:
    """Local Ollama adapter for structured meeting summarization."""

    def __init__(
        self,
        *,
        model: str = OLLAMA_DEFAULT_MODEL,
        endpoint: str = OLLAMA_DEFAULT_ENDPOINT,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.metadata = SummarizationProviderMetadata(
            name="ollama",
            model=model,
            endpoint=_summarization_local_ollama_endpoint(endpoint),
            num_ctx=OLLAMA_SUMMARIZATION_NUM_CTX,
            temperature=OLLAMA_SUMMARIZATION_TEMPERATURE,
            seed=OLLAMA_SUMMARIZATION_SEED,
        )
        self.timeout_seconds = timeout_seconds
        self._runtime_resource_used = False

    @property
    def runtime_resource_used(self) -> bool:
        return self._runtime_resource_used

    def summarize(self, request: SummarizationRequest) -> SummaryResult:
        records: list[ProductRequestRecord] = []
        payloads = {
            "timeline": self._run_product(
                product="timeline",
                schema=TIMELINE_RESPONSE_SCHEMA,
                request=request,
                records=records,
            ),
            "action_items": self._run_product(
                product="action_items",
                schema=ACTION_ITEMS_RESPONSE_SCHEMA,
                request=request,
                records=records,
            ),
            "general_summary": self._run_product(
                product="general_summary",
                schema=GENERAL_SUMMARY_RESPONSE_SCHEMA,
                request=request,
                records=records,
            ),
        }
        result = assemble_summary_result(product_payloads=payloads, request_records=records)
        assert_summary_output_language(result, request.policy.language)
        return result

    def _run_product(
        self,
        *,
        product: str,
        schema: Mapping[str, object],
        request: SummarizationRequest,
        records: list[ProductRequestRecord],
    ) -> Mapping[str, object]:
        system_content = product_system_prompt(product, request.policy.language)
        user_content = product_user_prompt(request)
        guard_summarization_token_budget(
            product=product,
            system_content=system_content,
            user_content=user_content,
            num_ctx=OLLAMA_SUMMARIZATION_NUM_CTX,
        )
        payload = {
            "model": self.metadata.model,
            "stream": False,
            "format": schema,
            "options": OLLAMA_DETERMINISTIC_OPTIONS,
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
        }
        response = self._post_json("/api/chat", payload)
        content = _summarization_ollama_message_content(response)
        _assert_ollama_response_not_truncated(
            product=product,
            response=response,
            content=content,
        )
        try:
            decoded = json.loads(strip_json_fence(content))
        except json.JSONDecodeError as exc:
            raise SummarizationContractError(
                _with_raw_provider_message_content(
                    f"Ollama {product} response was not valid JSON.",
                    content,
                )
            ) from exc
        payload_mapping = _mapping(decoded, f"Ollama {product} response")
        try:
            validate_product_payload(product, payload_mapping)
        except SummarizationContractError as exc:
            raise SummarizationContractError(
                _with_raw_provider_message_content(str(exc), content)
            ) from exc
        prompt_eval_count, eval_count = _summarization_ollama_token_counts(
            product=product,
            response=response,
        )
        records.append(
            ProductRequestRecord(
                product=product,
                model=self.metadata.model,
                num_ctx=OLLAMA_SUMMARIZATION_NUM_CTX,
                temperature=OLLAMA_SUMMARIZATION_TEMPERATURE,
                seed=OLLAMA_SUMMARIZATION_SEED,
                prompt_eval_count=prompt_eval_count,
                eval_count=eval_count,
            )
        )
        return payload_mapping

    def _post_json(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        try:
            if path == "/api/chat":
                self._runtime_resource_used = True
            return post_ollama_json(
                endpoint=str(self.metadata.endpoint),
                path=path,
                payload=payload,
                timeout_seconds=self.timeout_seconds,
            )
        except LocalLLMProviderUnavailable as exc:
            raise SummarizationProviderUnavailable(
                "Ollama summarization provider is unavailable."
            ) from exc
        except LocalLLMContractError as exc:
            raise SummarizationContractError(str(exc)) from exc

    def release_runtime_resource(self, lease: RuntimeLease) -> RuntimeReleaseResult:
        """Release this provider's local Ollama model residency."""

        try:
            done_reason = unload_ollama_model(
                endpoint=str(self.metadata.endpoint),
                model=self.metadata.model,
                timeout_seconds=self.timeout_seconds,
            )
        except LocalLLMProviderUnavailable as exc:
            raise SummarizationProviderUnavailable(
                "Ollama summarization provider is unavailable during release."
            ) from exc
        except LocalLLMContractError as exc:
            raise SummarizationContractError(str(exc)) from exc
        return RuntimeReleaseResult(
            lease=lease,
            status="released",
            detail=f"Ollama model release completed: {done_reason}.",
        )


def _summarization_ollama_token_counts(
    *,
    product: str,
    response: Mapping[str, object],
) -> tuple[int, int]:
    try:
        prompt_eval_count, eval_count = ollama_token_counts(response)
    except LocalLLMContractError as exc:
        raise SummarizationContractError(str(exc)) from exc
    missing = [
        name
        for name, value in (
            ("prompt_eval_count", prompt_eval_count),
            ("eval_count", eval_count),
        )
        if value is None
    ]
    if missing:
        raise SummarizationContractError(
            f"Ollama {product} response omitted required token counts: {', '.join(missing)}."
        )
    assert prompt_eval_count is not None
    assert eval_count is not None
    return prompt_eval_count, eval_count


def _assert_ollama_response_not_truncated(
    *,
    product: str,
    response: Mapping[str, object],
    content: str,
) -> None:
    if response.get("done_reason") == "length":
        raise SummarizationContractError(
            _with_raw_provider_message_content(
                f"Ollama {product} response was truncated by the generation token limit "
                "(done_reason=length).",
                content,
            )
        )


def _summarization_ollama_message_content(response: Mapping[str, object]) -> str:
    try:
        return ollama_message_content(response)
    except LocalLLMContractError as exc:
        raise SummarizationContractError(str(exc)) from exc


def _summarization_local_ollama_endpoint(endpoint: str) -> str:
    try:
        return local_ollama_endpoint(endpoint)
    except (LocalLLMContractError, ValueError) as exc:
        raise SummarizationContractError(str(exc)) from exc


def _with_raw_provider_message_content(message: str, content: str) -> str:
    return f"{message} Raw Ollama message content: {_diagnostic_excerpt(content)}"


def _diagnostic_excerpt(content: str, *, limit: int = 1000) -> str:
    stripped = content.strip()
    if len(stripped) <= limit:
        return stripped
    return f"{stripped[:limit]}... [truncated]"


def _mapping(data: object, label: str) -> Mapping[str, object]:
    if not isinstance(data, Mapping):
        raise SummarizationContractError(f"{label} must be a JSON object.")
    return data
