import json
from collections.abc import Mapping
from dataclasses import replace

import pytest

from mena.mistral_llm import (
    MENA_MISTRAL_API_KEY_ENV,
    MISTRAL_API_BASE_URL,
    MISTRAL_SUMMARY_ALLOWLIST_POLICY,
    MISTRAL_SUMMARY_REQUEST_MODEL,
)
from mena.mistral_summarization import (
    MISTRAL_SUMMARIZATION_CONTEXT_TOKENS,
    MISTRAL_SUMMARIZATION_SEED,
    MISTRAL_SUMMARIZATION_TEMPERATURE,
    MistralSummarizationProvider,
)
from mena.ollama_summarization import OllamaSummarizationProvider
from mena.summarization import (
    PROMPT_POLICY,
    SummarizationContractError,
    SummarizationError,
    SummarizationPolicy,
    SummarizationProvider,
    SummarizationProviderUnavailable,
    SummarizationTokenBudgetError,
)
from test_mistral_llm import API_KEY, _FakeTransport, _models_body
from test_summarization import _summary_request, _summary_result, _timeline_provider_payload

RESOLVED_MODEL = "mistral-small-2603"


def _chat_body(
    payload: Mapping[str, object],
    *,
    model: str = RESOLVED_MODEL,
    finish_reason: str = "stop",
    usage: Mapping[str, object] | None = None,
    content: str | None = None,
) -> bytes:
    return json.dumps(
        {
            "model": model,
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {"content": content or json.dumps(payload)},
                }
            ],
            "usage": (
                {"prompt_tokens": 321, "completion_tokens": 42, "total_tokens": 363}
                if usage is None
                else usage
            ),
        }
    ).encode("utf-8")


def _product_bodies(**overrides: bytes) -> list[tuple[int, bytes]]:
    result = _summary_result()
    defaults = {
        "timeline": _chat_body(_timeline_provider_payload(result)),
        "action_items": _chat_body(
            {"action_items": [action.to_dict() for action in result.action_items]}
        ),
        "general_summary": _chat_body({"general_summary": result.general_summary}),
    }
    return [(200, overrides.get(product, body)) for product, body in defaults.items()]


def _provider(transport: _FakeTransport) -> MistralSummarizationProvider:
    return MistralSummarizationProvider(
        api_key=API_KEY,
        resolved_model=RESOLVED_MODEL,
        transport=transport,
        sleep=lambda _seconds: None,
    )


def test_mistral_provider_satisfies_the_summarization_port() -> None:
    provider = _provider(_FakeTransport([]))

    assert isinstance(provider, SummarizationProvider)
    assert provider.metadata.name == "mistral"
    assert provider.metadata.model == MISTRAL_SUMMARY_REQUEST_MODEL
    assert provider.metadata.endpoint == MISTRAL_API_BASE_URL
    assert provider.metadata.prompt_policy == PROMPT_POLICY
    assert provider.metadata.resolved_model == RESOLVED_MODEL


def test_mistral_provider_runs_one_structured_call_per_product() -> None:
    transport = _FakeTransport(_product_bodies())
    provider = _provider(transport)

    result = provider.summarize(_summary_request())

    assert result.timeline[0].text == "Discussed database migration before Friday."
    assert result.action_items[0].owner == "You"
    assert result.general_summary == "The meeting focused on database migration risk."
    assert len(transport.calls) == 3
    products: list[str] = []
    for _method, _url, headers, body in transport.calls:
        assert headers["authorization"] == f"Bearer {API_KEY}"
        assert body is not None
        payload = json.loads(body)
        assert payload["model"] == MISTRAL_SUMMARY_REQUEST_MODEL
        assert payload["temperature"] == MISTRAL_SUMMARIZATION_TEMPERATURE
        assert payload["random_seed"] == MISTRAL_SUMMARIZATION_SEED
        response_format = payload["response_format"]
        assert response_format["type"] == "json_schema"
        assert response_format["json_schema"]["strict"] is True
        schema = response_format["json_schema"]["schema"]
        assert schema["additionalProperties"] is False
        products.append(schema["required"][0])
        messages = payload["messages"]
        assert messages[0]["role"] == "system"
        assert "meeting_notes" in messages[1]["content"]
    assert products == ["timeline", "action_items", "general_summary"]


def test_mistral_provider_sends_the_same_request_content_as_the_local_provider() -> None:
    from mena.summarization import product_user_prompt

    transport = _FakeTransport(_product_bodies())
    provider = _provider(transport)
    request = _summary_request()

    provider.summarize(request)

    _method, _url, _headers, body = transport.calls[0]
    assert body is not None
    payload = json.loads(body)
    assert payload["messages"][1]["content"] == product_user_prompt(request)


def test_mistral_provider_records_request_provenance_per_product() -> None:
    provider = _provider(_FakeTransport(_product_bodies()))

    result = provider.summarize(_summary_request())

    assert [record.product for record in result.request_records] == [
        "timeline",
        "action_items",
        "general_summary",
    ]
    for record in result.request_records:
        assert record.model == MISTRAL_SUMMARY_REQUEST_MODEL
        assert record.num_ctx == MISTRAL_SUMMARIZATION_CONTEXT_TOKENS
        assert record.temperature == MISTRAL_SUMMARIZATION_TEMPERATURE
        assert record.seed == MISTRAL_SUMMARIZATION_SEED
        assert record.prompt_eval_count == 321
        assert record.eval_count == 42


def test_mistral_provider_config_identity_differs_from_ollama() -> None:
    mistral = _provider(_FakeTransport([]))
    ollama = OllamaSummarizationProvider(model="llama3.1:8b")

    assert mistral.metadata.config_id != ollama.metadata.config_id
    assert mistral.metadata.to_dict()["resolved_model"] == RESOLVED_MODEL
    assert "resolved_model" not in ollama.metadata.to_dict()


def test_mistral_provider_reuse_identity_changes_with_resolved_model() -> None:
    first = MistralSummarizationProvider(api_key=API_KEY, resolved_model="mistral-small-2603")
    second = MistralSummarizationProvider(api_key=API_KEY, resolved_model=None)

    assert first.metadata.config_id != second.metadata.config_id


def test_mistral_provider_requires_the_mena_scoped_api_key() -> None:
    provider = MistralSummarizationProvider(api_key=None)

    with pytest.raises(SummarizationError, match=MENA_MISTRAL_API_KEY_ENV):
        provider.summarize(_summary_request())


def test_mistral_provider_never_leaks_the_api_key_into_errors() -> None:
    transport = _FakeTransport([(401, json.dumps({"message": f"bad {API_KEY}"}).encode("utf-8"))])
    provider = _provider(transport)

    with pytest.raises(SummarizationError) as excinfo:
        provider.summarize(_summary_request())

    assert API_KEY not in str(excinfo.value)


def test_mistral_provider_maps_connection_failures_to_provider_unavailable() -> None:
    transport = _FakeTransport([OSError("down"), OSError("down"), OSError("down")])
    provider = _provider(transport)

    with pytest.raises(SummarizationProviderUnavailable, match="unavailable"):
        provider.summarize(_summary_request())


def test_mistral_provider_maps_auth_failures_to_hard_failures() -> None:
    transport = _FakeTransport([(401, b'{"message": "Unauthorized"}')])
    provider = _provider(transport)

    with pytest.raises(SummarizationError, match="rejected the Mena-scoped API key") as excinfo:
        provider.summarize(_summary_request())

    assert not isinstance(excinfo.value, SummarizationProviderUnavailable)


def test_mistral_provider_maps_quota_failures_to_hard_failures() -> None:
    transport = _FakeTransport([(429, b'{"message": "insufficient credits"}')])
    provider = _provider(transport)

    with pytest.raises(SummarizationError, match="quota/funds") as excinfo:
        provider.summarize(_summary_request())

    assert not isinstance(excinfo.value, SummarizationProviderUnavailable)


def test_mistral_provider_rejects_non_json_content() -> None:
    bodies = _product_bodies(
        timeline=_chat_body({}, content="not json at all"),
    )
    provider = _provider(_FakeTransport(bodies))

    with pytest.raises(SummarizationContractError, match="not valid JSON"):
        provider.summarize(_summary_request())


def test_mistral_provider_rejects_schema_violating_content() -> None:
    bodies = _product_bodies(timeline=_chat_body({"timeline": []}))
    provider = _provider(_FakeTransport(bodies))

    with pytest.raises(SummarizationContractError, match="timeline must not be empty"):
        provider.summarize(_summary_request())


def test_mistral_provider_rejects_truncated_responses() -> None:
    bodies = _product_bodies(
        timeline=_chat_body(
            {"timeline": [{"text": "x", "source_segment_ids": ["merged_segment_000001"]}]},
            finish_reason="length",
        ),
    )
    provider = _provider(_FakeTransport(bodies))

    with pytest.raises(SummarizationContractError, match="finish_reason=length"):
        provider.summarize(_summary_request())


def test_mistral_provider_requires_token_counts() -> None:
    bodies = _product_bodies(
        timeline=_chat_body(
            {"timeline": [{"text": "x", "source_segment_ids": ["merged_segment_000001"]}]},
            usage={"prompt_tokens": 1},
        ),
    )
    provider = _provider(_FakeTransport(bodies))

    with pytest.raises(SummarizationContractError, match="completion_tokens"):
        provider.summarize(_summary_request())


def test_mistral_provider_fails_closed_when_the_serving_model_drifts() -> None:
    bodies = _product_bodies(
        timeline=_chat_body(
            {"timeline": [{"text": "x", "source_segment_ids": ["merged_segment_000001"]}]},
            model="mistral-large-2512",
        ),
    )
    provider = _provider(_FakeTransport(bodies))

    with pytest.raises(SummarizationError, match=MISTRAL_SUMMARY_ALLOWLIST_POLICY):
        provider.summarize(_summary_request())


def test_mistral_provider_reverifies_every_alias_echo_before_accepting_it() -> None:
    result = _summary_result()
    allowed_models = _models_body(
        {"id": "mistral-small-2603", "aliases": [MISTRAL_SUMMARY_REQUEST_MODEL]}
    )
    bodies = [
        (
            200,
            _chat_body(_timeline_provider_payload(result), model=MISTRAL_SUMMARY_REQUEST_MODEL),
        ),
        (200, allowed_models),
        (
            200,
            _chat_body(
                {"action_items": [action.to_dict() for action in result.action_items]},
                model=MISTRAL_SUMMARY_REQUEST_MODEL,
            ),
        ),
        (200, allowed_models),
        (200, _chat_body({"general_summary": result.general_summary})),
    ]
    transport = _FakeTransport(bodies)
    provider = _provider(transport)

    assert provider.summarize(_summary_request()).timeline == result.timeline
    # Every alias-echoing response is re-verified through /v1/models: the
    # alias can drift between the per-product calls of one run, so earlier
    # evidence is never reused. Dated responses need no re-verification.
    models_calls = [call for call in transport.calls if call[1].endswith("/v1/models")]
    assert len(models_calls) == 2


def test_mistral_provider_fails_closed_when_the_alias_drifts_between_products() -> None:
    result = _summary_result()
    allowed_models = _models_body(
        {"id": "mistral-small-2603", "aliases": [MISTRAL_SUMMARY_REQUEST_MODEL]}
    )
    drifted_models = _models_body(
        {"id": "mistral-small-2707", "aliases": [MISTRAL_SUMMARY_REQUEST_MODEL]}
    )
    bodies = [
        (
            200,
            _chat_body(_timeline_provider_payload(result), model=MISTRAL_SUMMARY_REQUEST_MODEL),
        ),
        (200, allowed_models),
        (
            200,
            _chat_body(
                {"action_items": [action.to_dict() for action in result.action_items]},
                model=MISTRAL_SUMMARY_REQUEST_MODEL,
            ),
        ),
        (200, drifted_models),
    ]
    provider = _provider(_FakeTransport(bodies))

    with pytest.raises(SummarizationError, match="mistral-small-2707"):
        provider.summarize(_summary_request())


def test_mistral_provider_rejects_an_alias_echo_that_resolved_outside_the_allowlist() -> None:
    result = _summary_result()
    drifted_models = _models_body(
        {"id": "mistral-small-2707", "aliases": [MISTRAL_SUMMARY_REQUEST_MODEL]}
    )
    bodies = [
        (
            200,
            _chat_body(_timeline_provider_payload(result), model=MISTRAL_SUMMARY_REQUEST_MODEL),
        ),
        (200, drifted_models),
    ]
    provider = _provider(_FakeTransport(bodies))

    with pytest.raises(SummarizationError, match="mistral-small-2707") as excinfo:
        provider.summarize(_summary_request())

    assert MISTRAL_SUMMARY_ALLOWLIST_POLICY in str(excinfo.value)


def test_mistral_provider_reports_unavailable_when_alias_reverification_cannot_run() -> None:
    result = _summary_result()
    bodies: list[tuple[int, bytes] | Exception] = [
        (
            200,
            _chat_body(_timeline_provider_payload(result), model=MISTRAL_SUMMARY_REQUEST_MODEL),
        ),
        OSError("down"),
        OSError("down"),
        OSError("down"),
    ]
    provider = _provider(_FakeTransport(bodies))

    with pytest.raises(SummarizationProviderUnavailable, match="re-verifying"):
        provider.summarize(_summary_request())


def test_mistral_provider_scopes_reuse_identity_by_account() -> None:
    first = MistralSummarizationProvider(api_key="mena-key-one")
    second = MistralSummarizationProvider(api_key="mena-key-two")
    keyless = MistralSummarizationProvider(api_key=None)

    assert first.metadata.account_scope is not None
    assert first.metadata.account_scope != second.metadata.account_scope
    assert first.metadata.config_id != second.metadata.config_id
    assert keyless.metadata.account_scope is None
    # The non-secret scope never exposes key material.
    payload = json.dumps(first.metadata.to_dict())
    assert "mena-key-one" not in payload
    assert "account_scope" not in OllamaSummarizationProvider(model="m").metadata.to_dict()


def test_mistral_provider_guards_the_context_budget_before_sending() -> None:
    transport = _FakeTransport([])
    provider = _provider(transport)
    request = _summary_request()
    merged = request.merged_transcript
    oversized = replace(
        request,
        merged_transcript=replace(
            merged,
            segments=(
                replace(
                    merged.segments[0],
                    text="a" * (MISTRAL_SUMMARIZATION_CONTEXT_TOKENS + 1),
                ),
            )
            + merged.segments[1:],
        ),
    )

    with pytest.raises(SummarizationTokenBudgetError, match="refusing to send"):
        provider.summarize(oversized)

    assert transport.calls == []


def test_mistral_provider_enforces_the_output_language_guard() -> None:
    english = _summary_result()
    bodies = [
        (200, _chat_body(_timeline_provider_payload(english))),
        (
            200,
            _chat_body({"action_items": [action.to_dict() for action in english.action_items]}),
        ),
        (
            200,
            _chat_body(
                {
                    "general_summary": (
                        "The meeting focused on the database migration and the risks "
                        "that we have discussed before the release for this team."
                    )
                }
            ),
        ),
    ]
    provider = _provider(_FakeTransport(bodies))
    request = replace(_summary_request(), policy=SummarizationPolicy(language="de"))

    with pytest.raises(SummarizationContractError, match="drifted to English"):
        provider.summarize(request)
