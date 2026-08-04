"""Local Ollama adapter for structured meeting note-package summarization."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Self

from mena.local_llm import (
    OLLAMA_DEFAULT_ENDPOINT,
    OLLAMA_DEFAULT_MODEL,
    LocalLLMContractError,
    LocalLLMProviderUnavailable,
    LocalLLMTimeout,
    local_ollama_endpoint,
    ollama_message_content,
    ollama_token_counts,
    post_ollama_json,
    strip_json_fence,
    unload_ollama_model,
)
from mena.merge_timing import MergedTranscriptSegment
from mena.note_package import (
    PRODUCT_MAP_RESPONSE_SCHEMAS,
    PRODUCT_RESPONSE_SCHEMAS,
    ProductRequestRecord,
    SummarizationContractError,
    SummarizationRequest,
    SummarizationTokenBudgetError,
    SummaryResult,
    assemble_summary_result,
    assert_summary_output_language,
    product_system_prompt,
    product_user_prompt,
    schema_grounding,
    validate_product_map_payload,
    validate_product_payload,
)
from mena.note_product_strategy import (
    MAP_CALL,
    ONE_SHOT_CALL,
    ONE_SHOT_STRATEGY,
    REDUCE_CALL,
    REDUCER_STRATEGY,
    TOKEN_BUDGET_FRACTION,
    NoteProductStrategyError,
    estimate_prompt_tokens,
    plan_chunk_spans,
    prompt_token_budget,
    select_note_product_strategy,
)
from mena.runtime_stewardship import RuntimeLease, RuntimeReleaseResult
from mena.summarization import (
    SummarizationError,
    SummarizationProviderMetadata,
    SummarizationProviderUnavailable,
    SummarizationTimeout,
)

OLLAMA_SUMMARIZATION_NUM_CTX = 8192
OLLAMA_SUMMARIZATION_SEED = 0
OLLAMA_SUMMARIZATION_TEMPERATURE = 0
OLLAMA_SUMMARIZATION_GENERATION_TIMEOUT_SECONDS = 600.0
OLLAMA_SUMMARIZATION_RELEASE_TIMEOUT_SECONDS = 60.0
OLLAMA_DETERMINISTIC_OPTIONS: dict[str, object] = {
    "temperature": OLLAMA_SUMMARIZATION_TEMPERATURE,
    "seed": OLLAMA_SUMMARIZATION_SEED,
    "num_ctx": OLLAMA_SUMMARIZATION_NUM_CTX,
}


def _generation_timeout_message() -> str:
    minutes = OLLAMA_SUMMARIZATION_GENERATION_TIMEOUT_SECONDS / 60
    return f"Local Ollama summarization timed out after the {minutes:g}-minute request ceiling."


@dataclass(frozen=True)
class _OllamaProductCall:
    """Validated envelope of one Ollama note-product chat call."""

    payload: Mapping[str, object]
    content: str
    prompt_eval_count: int
    eval_count: int


@dataclass(frozen=True)
class _ProductGenerationInputs:
    """Product-independent inputs computed once per summarize run."""

    request: SummarizationRequest
    one_shot_user_content: str
    one_shot_user_estimate: int
    segment_item_estimates: tuple[int, ...]

    @classmethod
    def for_request(cls, request: SummarizationRequest) -> Self:
        user_content = product_user_prompt(request)
        return cls(
            request=request,
            one_shot_user_content=user_content,
            one_shot_user_estimate=estimate_prompt_tokens(user_content),
            segment_item_estimates=_segment_item_estimates(request.merged_transcript.segments),
        )


def _segment_item_estimates(segments: tuple[MergedTranscriptSegment, ...]) -> tuple[int, ...]:
    # +2 reserves room for the JSON list separators around each segment.
    return tuple(
        estimate_prompt_tokens(
            json.dumps(_segment_payload(segment), ensure_ascii=False, sort_keys=True)
        )
        + 2
        for segment in segments
    )


def _validate_call_payload(
    validate: Callable[[str, Mapping[str, object]], None],
    product: str,
    call: _OllamaProductCall,
) -> None:
    try:
        validate(product, call.payload)
    except SummarizationContractError as exc:
        raise SummarizationContractError(
            _with_raw_provider_message_content(str(exc), call.content)
        ) from exc


class OllamaSummarizationProvider:
    """Local Ollama adapter for structured meeting summarization.

    The adapter owns the note-product execution strategy mechanics (ADR-035):
    per product it either sends one one-shot request or, when the one-shot
    prompt exceeds the ``num_ctx`` token budget, runs a chunked map/reduce
    over the merged transcript. Provider selection stays with the capability
    resolver; this class never chooses providers.
    """

    def __init__(
        self,
        *,
        model: str = OLLAMA_DEFAULT_MODEL,
        endpoint: str = OLLAMA_DEFAULT_ENDPOINT,
    ) -> None:
        self.metadata = SummarizationProviderMetadata(
            name="ollama",
            model=model,
            endpoint=_summarization_local_ollama_endpoint(endpoint),
            num_ctx=OLLAMA_SUMMARIZATION_NUM_CTX,
            temperature=OLLAMA_SUMMARIZATION_TEMPERATURE,
            seed=OLLAMA_SUMMARIZATION_SEED,
        )
        self._runtime_resource_used = False

    @property
    def runtime_resource_used(self) -> bool:
        return self._runtime_resource_used

    def summarize(self, request: SummarizationRequest) -> SummaryResult:
        records: list[ProductRequestRecord] = []
        inputs = _ProductGenerationInputs.for_request(request)
        payloads = {
            product: self._run_product(
                product=product,
                schema=PRODUCT_RESPONSE_SCHEMAS[product],
                inputs=inputs,
                records=records,
            )
            for product in PRODUCT_RESPONSE_SCHEMAS
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
        inputs: _ProductGenerationInputs,
        records: list[ProductRequestRecord],
    ) -> Mapping[str, object]:
        request = inputs.request
        system_content = product_system_prompt(product, request.policy.language, schema)
        strategy = select_note_product_strategy(
            prompt_estimate=(
                estimate_prompt_tokens(system_content) + inputs.one_shot_user_estimate
            ),
            prompt_budget=_summarization_prompt_budget(),
        )
        if strategy == ONE_SHOT_STRATEGY:
            return self._run_one_shot_product(
                product=product,
                schema=schema,
                system_content=system_content,
                user_content=inputs.one_shot_user_content,
                records=records,
            )
        return self._run_reducer_product(
            product=product,
            schema=schema,
            inputs=inputs,
            records=records,
        )

    def _run_one_shot_product(
        self,
        *,
        product: str,
        schema: Mapping[str, object],
        system_content: str,
        user_content: str,
        records: list[ProductRequestRecord],
    ) -> Mapping[str, object]:
        call = self._post_product_call(
            label=product,
            schema=schema,
            system_content=system_content,
            user_content=user_content,
        )
        _validate_call_payload(validate_product_payload, product, call)
        records.append(
            self._request_record(
                product=product,
                strategy=ONE_SHOT_STRATEGY,
                call_kind=ONE_SHOT_CALL,
                call=call,
            )
        )
        return call.payload

    def _run_reducer_product(
        self,
        *,
        product: str,
        schema: Mapping[str, object],
        inputs: _ProductGenerationInputs,
        records: list[ProductRequestRecord],
    ) -> Mapping[str, object]:
        request = inputs.request
        language = request.policy.language
        reduce_system = _reduce_system_prompt(product, language, schema)
        _guard_reduce_preflight_budget(
            product=product,
            reduce_system_content=reduce_system,
            request=request,
        )
        map_schema = PRODUCT_MAP_RESPONSE_SCHEMAS[product]
        map_system = _map_system_prompt(product, language, map_schema)
        chunks = _plan_reducer_chunks(
            product=product,
            request=request,
            map_system_content=map_system,
            item_estimates=inputs.segment_item_estimates,
        )
        chunk_count = len(chunks)
        candidates: list[dict[str, object]] = []
        for chunk_index, segments in chunks:
            call = self._post_product_call(
                label=f"{product} map {chunk_index + 1}/{chunk_count}",
                schema=map_schema,
                system_content=map_system,
                user_content=_map_user_prompt(
                    request=request,
                    chunk_index=chunk_index,
                    chunk_count=chunk_count,
                    segments=segments,
                ),
            )
            _validate_call_payload(validate_product_map_payload, product, call)
            _validate_timeline_map_source_segments(
                product=product,
                payload=call.payload,
                segments=segments,
            )
            records.append(
                self._request_record(
                    product=product,
                    strategy=REDUCER_STRATEGY,
                    call_kind=MAP_CALL,
                    call=call,
                    chunk_index=chunk_index,
                    chunk_count=chunk_count,
                )
            )
            candidates.append({"chunk_index": chunk_index, product: call.payload.get(product)})
        _require_timeline_grounding(product, candidates, request.manual_notes)
        reduce_call = self._post_product_call(
            label=f"{product} reduce",
            schema=schema,
            system_content=reduce_system,
            user_content=_reduce_user_prompt(request=request, candidates=candidates),
        )
        _validate_call_payload(validate_product_payload, product, reduce_call)
        records.append(
            self._request_record(
                product=product,
                strategy=REDUCER_STRATEGY,
                call_kind=REDUCE_CALL,
                call=reduce_call,
                chunk_count=chunk_count,
            )
        )
        return reduce_call.payload

    def _post_product_call(
        self,
        *,
        label: str,
        schema: Mapping[str, object],
        system_content: str,
        user_content: str,
    ) -> _OllamaProductCall:
        _guard_token_budget(
            label=label,
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
            label=label,
            response=response,
            content=content,
        )
        try:
            decoded = json.loads(strip_json_fence(content))
        except json.JSONDecodeError as exc:
            raise SummarizationContractError(
                _with_raw_provider_message_content(
                    f"Ollama {label} response was not valid JSON.",
                    content,
                )
            ) from exc
        payload_mapping = _mapping(decoded, f"Ollama {label} response")
        prompt_eval_count, eval_count = _summarization_ollama_token_counts(
            label=label,
            response=response,
        )
        return _OllamaProductCall(
            payload=payload_mapping,
            content=content,
            prompt_eval_count=prompt_eval_count,
            eval_count=eval_count,
        )

    def _request_record(
        self,
        *,
        product: str,
        strategy: str,
        call_kind: str,
        call: _OllamaProductCall,
        chunk_index: int | None = None,
        chunk_count: int | None = None,
    ) -> ProductRequestRecord:
        return ProductRequestRecord(
            product=product,
            model=self.metadata.model,
            num_ctx=OLLAMA_SUMMARIZATION_NUM_CTX,
            temperature=OLLAMA_SUMMARIZATION_TEMPERATURE,
            seed=OLLAMA_SUMMARIZATION_SEED,
            prompt_eval_count=call.prompt_eval_count,
            eval_count=call.eval_count,
            strategy=strategy,
            call=call_kind,
            chunk_index=chunk_index,
            chunk_count=chunk_count,
        )

    def _post_json(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        try:
            if path == "/api/chat":
                self._runtime_resource_used = True
            return post_ollama_json(
                endpoint=str(self.metadata.endpoint),
                path=path,
                payload=payload,
                timeout_seconds=OLLAMA_SUMMARIZATION_GENERATION_TIMEOUT_SECONDS,
            )
        except LocalLLMTimeout as exc:
            raise SummarizationTimeout(_generation_timeout_message()) from exc
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
                timeout_seconds=OLLAMA_SUMMARIZATION_RELEASE_TIMEOUT_SECONDS,
            )
        except LocalLLMTimeout as exc:
            raise SummarizationError(
                "Ollama model release timed out at the terminal cleanup boundary."
            ) from exc
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


_MAP_INSTRUCTIONS: dict[str, str] = {
    "timeline": (
        "Return only key timeline (array of objects with text and source_segment_ids). "
        "Each entry is a meaningful chronological event from this transcript chunk, "
        "not a bare date; source_segment_ids must cite the provided segment id values; "
        "return an empty array when the chunk contains no meaningful events."
    ),
    "action_items": (
        "Return only key action_items (array of objects with text, owner, due). "
        "Each item is an actual task stated in this transcript chunk, not a topic "
        "label; owner and due may be null; return an empty array when the chunk "
        "contains no tasks."
    ),
    "general_summary": (
        "Return only key general_summary (string): a concise summary of what "
        "happened in this transcript chunk."
    ),
}

_REDUCE_INSTRUCTIONS: dict[str, str] = {
    "timeline": (
        "Return only key timeline (array of objects with text and source_segment_ids): "
        "merge the chunk timeline candidates into one chronological timeline for the "
        "whole meeting, preserving cited source_segment_ids, chunk order, and dropping "
        "duplicates."
    ),
    "action_items": (
        "Return only key action_items (array of objects with text, owner, due): "
        "merge the chunk action-item candidates into one deduplicated task list; "
        "keep owner and due when stated; drop entries that are topic labels "
        "rather than actual tasks."
    ),
    "general_summary": (
        "Return only key general_summary (string): one concise high-level "
        "overview of the whole meeting composed from the chunk summaries."
    ),
}


def _map_system_prompt(product: str, language: str, schema: Mapping[str, object]) -> str:
    return (
        "You extract candidate meeting notes from one chunk of a longer meeting "
        "transcript. "
        f"{_MAP_INSTRUCTIONS[product]} "
        "Be faithful to the transcript chunk. "
        f"Write every user-facing string in {language}. "
        f"{schema_grounding(schema)}"
    )


def _reduce_system_prompt(product: str, language: str, schema: Mapping[str, object]) -> str:
    return (
        "You combine candidate meeting notes extracted from consecutive chunks "
        "of one meeting transcript. "
        f"{_REDUCE_INSTRUCTIONS[product]} "
        "Use only the provided chunk candidates and manual notes; do not invent "
        "content. "
        f"Write every user-facing string in {language}. "
        f"{schema_grounding(schema)}"
    )


def _segment_payload(segment: MergedTranscriptSegment) -> dict[str, object]:
    return {
        "id": segment.id,
        "speaker": segment.speaker.label,
        "start_seconds": segment.start_seconds,
        "end_seconds": segment.end_seconds,
        "text": segment.text,
    }


def _map_user_prompt(
    *,
    request: SummarizationRequest,
    chunk_index: int,
    chunk_count: int,
    segments: tuple[MergedTranscriptSegment, ...],
) -> str:
    return json.dumps(
        {
            "task": "meeting_notes_chunk",
            "language": request.policy.language,
            "output_language": _output_language_instruction(request.policy.language),
            "chunk_index": chunk_index,
            "chunk_count": chunk_count,
            "transcript": {
                "segments": [_segment_payload(segment) for segment in segments],
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _reduce_user_prompt(
    *,
    request: SummarizationRequest,
    candidates: list[dict[str, object]],
) -> str:
    return json.dumps(
        {
            "task": "meeting_notes_reduce",
            "language": request.policy.language,
            "output_language": _output_language_instruction(request.policy.language),
            "chunk_candidates": candidates,
            "manual_notes": request.manual_notes,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _output_language_instruction(language: str) -> str:
    return (
        f"Write all user-facing strings in {language}: timeline entries, "
        "action item text, generated owner/due wording, and the general summary."
    )


def _require_timeline_grounding(
    product: str,
    candidates: list[dict[str, object]],
    manual_notes: str | None,
) -> None:
    if product != "timeline":
        return
    if any(candidate.get(product) for candidate in candidates):
        return
    if manual_notes and manual_notes.strip():
        return
    raise SummarizationContractError(
        "Ollama timeline map calls returned no timeline candidates and no "
        "manual notes are available; refusing to reduce an empty timeline."
    )


def _validate_timeline_map_source_segments(
    *,
    product: str,
    payload: Mapping[str, object],
    segments: tuple[MergedTranscriptSegment, ...],
) -> None:
    if product != "timeline":
        return

    allowed_ids = {segment.id for segment in segments}
    timeline = payload.get("timeline")
    if not isinstance(timeline, list):
        return
    for entry in timeline:
        if not isinstance(entry, Mapping):
            return
        source_segment_ids = entry.get("source_segment_ids")
        if not isinstance(source_segment_ids, list):
            return
        out_of_chunk = [
            source_id
            for source_id in source_segment_ids
            if isinstance(source_id, str) and source_id not in allowed_ids
        ]
        if out_of_chunk:
            raise SummarizationContractError(
                "Summary timeline chunk candidates source_segment_ids must cite "
                "segments from the same reducer chunk; out-of-chunk ids: "
                f"{', '.join(out_of_chunk)}."
            )


def _summarization_prompt_budget() -> int:
    return prompt_token_budget(
        num_ctx=OLLAMA_SUMMARIZATION_NUM_CTX,
        budget_fraction=TOKEN_BUDGET_FRACTION,
    )


def _guard_token_budget(
    *,
    label: str,
    system_content: str,
    user_content: str,
    num_ctx: int,
) -> None:
    estimate = estimate_prompt_tokens(system_content) + estimate_prompt_tokens(user_content)
    budget = _summarization_prompt_budget()
    if estimate > budget:
        raise SummarizationTokenBudgetError(
            f"Estimated {label} prompt tokens ({estimate}) exceed the configured "
            f"num_ctx budget ({budget} of {num_ctx}); refusing to send to avoid "
            "silent context truncation."
        )


def _guard_reduce_preflight_budget(
    *,
    product: str,
    reduce_system_content: str,
    request: SummarizationRequest,
) -> None:
    floor_estimate = estimate_prompt_tokens(reduce_system_content) + estimate_prompt_tokens(
        _reduce_user_prompt(request=request, candidates=[])
    )
    budget = _summarization_prompt_budget()
    if floor_estimate > budget:
        raise SummarizationTokenBudgetError(
            f"Estimated {product} reduce prompt tokens ({floor_estimate}) exceed the "
            f"configured num_ctx budget ({budget} of {OLLAMA_SUMMARIZATION_NUM_CTX}) "
            "before any chunk candidates; manual notes and reduction overhead are "
            "too large for the reducer strategy."
        )


_CHUNK_ENVELOPE_MARGIN = 64


def _plan_reducer_chunks(
    *,
    product: str,
    request: SummarizationRequest,
    map_system_content: str,
    item_estimates: tuple[int, ...],
) -> tuple[tuple[int, tuple[MergedTranscriptSegment, ...]], ...]:
    segments = request.merged_transcript.segments
    envelope = _map_user_prompt(
        request=request,
        chunk_index=0,
        chunk_count=0,
        segments=(),
    )
    chunk_budget = (
        _summarization_prompt_budget()
        - estimate_prompt_tokens(map_system_content)
        - estimate_prompt_tokens(envelope)
        - _CHUNK_ENVELOPE_MARGIN
    )
    if chunk_budget < 1:
        raise SummarizationTokenBudgetError(
            f"Estimated {product} map prompt overhead exceeds the configured "
            "num_ctx budget; no room is left for transcript segments."
        )
    try:
        spans = plan_chunk_spans(item_estimates=item_estimates, chunk_budget=chunk_budget)
    except NoteProductStrategyError as exc:
        raise SummarizationTokenBudgetError(
            f"Estimated {product} chunk prompt tokens exceed the configured num_ctx budget: {exc}"
        ) from exc
    return tuple((span.index, segments[span.start : span.end]) for span in spans)


def _summarization_ollama_token_counts(
    *,
    label: str,
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
            f"Ollama {label} response omitted required token counts: {', '.join(missing)}."
        )
    assert prompt_eval_count is not None
    assert eval_count is not None
    return prompt_eval_count, eval_count


def _assert_ollama_response_not_truncated(
    *,
    label: str,
    response: Mapping[str, object],
    content: str,
) -> None:
    if response.get("done_reason") == "length":
        raise SummarizationContractError(
            _with_raw_provider_message_content(
                f"Ollama {label} response was truncated by the generation token limit "
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
