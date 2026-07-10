"""Direct Mistral adapter behind Mena's existing summarization provider port.

Peer provider to :class:`mena.ollama_summarization.OllamaSummarizationProvider`: same
provider-independent request content, same per-product structured-output
schemas, same ``SummaryResult`` contract, same provider metadata shape. Only
the transport, request envelope, and error classification are Mistral-owned
(ADR-033).
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping

from mena.local_llm import strip_json_fence
from mena.mistral_llm import (
    MENA_MISTRAL_API_KEY_ENV,
    MISTRAL_API_BASE_URL,
    MISTRAL_REQUEST_TIMEOUT_SECONDS,
    MISTRAL_SUMMARY_ALLOWED_RESOLVED_MODELS,
    MISTRAL_SUMMARY_ALLOWLIST_POLICY,
    MISTRAL_SUMMARY_REQUEST_MODEL,
    MistralAuthError,
    MistralContractError,
    MistralModelResolution,
    MistralQuotaError,
    MistralUnavailableError,
    SleepFn,
    Transport,
    mistral_account_scope,
    mistral_chat_completion,
    mistral_finish_reason,
    mistral_message_content,
    mistral_response_model,
    mistral_token_counts,
    resolve_mistral_summary_model,
)
from mena.note_package import (
    PRODUCT_RESPONSE_SCHEMA_NAMES,
    PRODUCT_RESPONSE_SCHEMAS,
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
from mena.summarization import (
    SummarizationError,
    SummarizationProviderMetadata,
    SummarizationProviderUnavailable,
)

MISTRAL_PROVIDER_NAME = "mistral"
# Both accepted Small 4 request/resolved IDs list a 256k context window; the
# value participates in the shared pre-send byte-estimate budget guard and in
# per-product request records, mirroring the Ollama ``num_ctx`` discipline.
MISTRAL_SUMMARIZATION_CONTEXT_TOKENS = 262144
MISTRAL_SUMMARIZATION_TEMPERATURE = 0
MISTRAL_SUMMARIZATION_SEED = 0


class MistralSummarizationProvider:
    """Fixed-model direct Mistral adapter for structured meeting summarization."""

    def __init__(
        self,
        *,
        api_key: str | None,
        resolved_model: str | None = None,
        timeout_seconds: float = MISTRAL_REQUEST_TIMEOUT_SECONDS,
        transport: Transport | None = None,
        sleep: SleepFn = time.sleep,
    ) -> None:
        self.metadata = SummarizationProviderMetadata(
            name=MISTRAL_PROVIDER_NAME,
            model=MISTRAL_SUMMARY_REQUEST_MODEL,
            endpoint=MISTRAL_API_BASE_URL,
            num_ctx=MISTRAL_SUMMARIZATION_CONTEXT_TOKENS,
            temperature=MISTRAL_SUMMARIZATION_TEMPERATURE,
            seed=MISTRAL_SUMMARIZATION_SEED,
            resolved_model=resolved_model,
            account_scope=None if api_key is None else mistral_account_scope(api_key),
        )
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._sleep = sleep

    def summarize(self, request: SummarizationRequest) -> SummaryResult:
        if self._api_key is None:
            raise SummarizationError(
                f"Mistral summarization requires {MENA_MISTRAL_API_KEY_ENV}; the "
                "Mena-scoped API key is not set."
            )
        records: list[ProductRequestRecord] = []
        payloads = {
            product: self._run_product(
                product=product,
                schema=schema,
                request=request,
                records=records,
            )
            for product, schema in PRODUCT_RESPONSE_SCHEMAS.items()
        }
        result = assemble_summary_result(
            product_payloads=payloads,
            request_records=records,
            merged_transcript=request.merged_transcript,
        )
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
        system_content = product_system_prompt(product, request.policy.language, schema)
        user_content = product_user_prompt(request)
        guard_summarization_token_budget(
            product=product,
            system_content=system_content,
            user_content=user_content,
            num_ctx=MISTRAL_SUMMARIZATION_CONTEXT_TOKENS,
        )
        assert self._api_key is not None
        try:
            response = mistral_chat_completion(
                api_key=self._api_key,
                model=self.metadata.model,
                messages=(
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": user_content},
                ),
                schema=schema,
                schema_name=PRODUCT_RESPONSE_SCHEMA_NAMES[product],
                temperature=MISTRAL_SUMMARIZATION_TEMPERATURE,
                random_seed=MISTRAL_SUMMARIZATION_SEED,
                timeout_seconds=self._timeout_seconds,
                transport=self._transport,
                sleep=self._sleep,
            )
        except MistralUnavailableError as exc:
            raise SummarizationProviderUnavailable(
                f"Mistral summarization provider is unavailable: {exc}"
            ) from exc
        except MistralAuthError as exc:
            raise SummarizationError(
                f"Mistral rejected the Mena-scoped API key for {product}: {exc}"
            ) from exc
        except MistralQuotaError as exc:
            raise SummarizationError(
                f"Mistral quota/funds failure during {product}: {exc}"
            ) from exc
        except MistralContractError as exc:
            raise SummarizationContractError(str(exc)) from exc
        return self._decode_product_response(
            product=product,
            response=response,
            records=records,
        )

    def _decode_product_response(
        self,
        *,
        product: str,
        response: Mapping[str, object],
        records: list[ProductRequestRecord],
    ) -> Mapping[str, object]:
        try:
            self._assert_response_model_allowed(product=product, response=response)
            if mistral_finish_reason(response) == "length":
                raise SummarizationContractError(
                    f"Mistral {product} response was truncated by the generation "
                    "token limit (finish_reason=length)."
                )
            content = mistral_message_content(response)
        except MistralContractError as exc:
            raise SummarizationContractError(str(exc)) from exc
        try:
            decoded = json.loads(strip_json_fence(content))
        except json.JSONDecodeError as exc:
            raise SummarizationContractError(
                f"Mistral {product} response was not valid JSON."
            ) from exc
        if not isinstance(decoded, Mapping):
            raise SummarizationContractError(f"Mistral {product} response must be a JSON object.")
        validate_product_payload(product, decoded)
        try:
            prompt_tokens, completion_tokens = mistral_token_counts(response)
        except MistralContractError as exc:
            raise SummarizationContractError(str(exc)) from exc
        records.append(
            ProductRequestRecord(
                product=product,
                model=self.metadata.model,
                num_ctx=MISTRAL_SUMMARIZATION_CONTEXT_TOKENS,
                temperature=MISTRAL_SUMMARIZATION_TEMPERATURE,
                seed=MISTRAL_SUMMARIZATION_SEED,
                prompt_eval_count=prompt_tokens,
                eval_count=completion_tokens,
            )
        )
        return decoded

    def _assert_response_model_allowed(
        self,
        *,
        product: str,
        response: Mapping[str, object],
    ) -> None:
        """Fail closed if the serving model drifted outside the allow-list.

        Setup/readiness already resolved the request alias, but the alias is
        mutable; the response-time model identity is the last line of defense
        against silent drift between readiness and the actual call. An echo of
        the mutable alias is not evidence, so it is re-resolved through
        ``/v1/models`` for every alias-echoing response — the alias can drift
        between the per-product calls of a single run, so earlier evidence is
        never reused.
        """

        response_model = mistral_response_model(response)
        if response_model in MISTRAL_SUMMARY_ALLOWED_RESOLVED_MODELS:
            return
        if response_model == self.metadata.model:
            resolution = self._verified_alias_resolution(product)
            if resolution.allowed:
                return
            raise SummarizationError(
                f"Mistral served {product} under the mutable alias "
                f"{response_model!r}, which currently resolves to "
                f"{resolution.resolved_model!r}, outside the accepted snapshot "
                f"under {MISTRAL_SUMMARY_ALLOWLIST_POLICY}; refusing the response "
                "until a new allow-list review accepts it."
            )
        raise SummarizationError(
            f"Mistral served {product} with model {response_model!r}, outside the "
            f"accepted snapshot under {MISTRAL_SUMMARY_ALLOWLIST_POLICY}; refusing "
            "the response until a new allow-list review accepts it."
        )

    def _verified_alias_resolution(self, product: str) -> MistralModelResolution:
        assert self._api_key is not None
        try:
            return resolve_mistral_summary_model(
                api_key=self._api_key,
                timeout_seconds=self._timeout_seconds,
                transport=self._transport,
                sleep=self._sleep,
            )
        except MistralUnavailableError as exc:
            raise SummarizationProviderUnavailable(
                "Mistral is unavailable while re-verifying the alias resolution "
                f"for {product}: {exc}"
            ) from exc
        except (MistralAuthError, MistralQuotaError) as exc:
            raise SummarizationError(
                f"Mistral alias re-verification failed for {product}: {exc}"
            ) from exc
        except MistralContractError as exc:
            raise SummarizationContractError(str(exc)) from exc
