import json
from collections.abc import Mapping, Sequence

import pytest

from mena.mistral_llm import (
    GENERIC_MISTRAL_API_KEY_ENV,
    MENA_MISTRAL_API_KEY_ENV,
    MISTRAL_API_BASE_URL,
    MISTRAL_CHAT_COMPLETIONS_PATH,
    MISTRAL_MODELS_PATH,
    MISTRAL_SUMMARY_ALLOWLIST_POLICY,
    MISTRAL_SUMMARY_REQUEST_MODEL,
    MistralAuthError,
    MistralContractError,
    MistralQuotaError,
    MistralUnavailableError,
    check_mistral_summary_readiness,
    load_mena_mistral_api_key,
    mistral_chat_completion,
    mistral_finish_reason,
    mistral_json_request,
    mistral_message_content,
    mistral_response_model,
    mistral_token_counts,
    resolve_mistral_summary_model,
)

API_KEY = "mena-test-key-123"


class _FakeTransport:
    """Scripted transport: replays (status, body) responses and records calls."""

    def __init__(self, responses: Sequence[tuple[int, bytes] | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> tuple[int, bytes]:
        self.calls.append((method, url, dict(headers), body))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _models_body(*entries: dict[str, object]) -> bytes:
    return json.dumps({"object": "list", "data": list(entries)}).encode("utf-8")


def _no_sleep(_seconds: float) -> None:
    return None


def test_load_mena_mistral_api_key_reads_only_the_mena_scoped_variable() -> None:
    assert load_mena_mistral_api_key({MENA_MISTRAL_API_KEY_ENV: " secret "}) == "secret"
    assert load_mena_mistral_api_key({MENA_MISTRAL_API_KEY_ENV: "   "}) is None
    assert load_mena_mistral_api_key({GENERIC_MISTRAL_API_KEY_ENV: "generic"}) is None
    assert load_mena_mistral_api_key({}) is None


def test_mistral_json_request_sends_bearer_auth_to_fixed_endpoint() -> None:
    transport = _FakeTransport([(200, b'{"ok": true}')])

    envelope = mistral_json_request(
        method="GET",
        path=MISTRAL_MODELS_PATH,
        api_key=API_KEY,
        transport=transport,
        sleep=_no_sleep,
    )

    assert envelope == {"ok": True}
    method, url, headers, body = transport.calls[0]
    assert method == "GET"
    assert url == f"{MISTRAL_API_BASE_URL}{MISTRAL_MODELS_PATH}"
    assert headers["authorization"] == f"Bearer {API_KEY}"
    assert body is None


def test_mistral_json_request_classifies_auth_failures_without_retry() -> None:
    transport = _FakeTransport([(401, b'{"message": "Unauthorized"}')])

    with pytest.raises(MistralAuthError, match="HTTP 401"):
        mistral_json_request(
            method="GET",
            path=MISTRAL_MODELS_PATH,
            api_key=API_KEY,
            transport=transport,
            sleep=_no_sleep,
        )

    assert len(transport.calls) == 1


def test_mistral_json_request_redacts_the_api_key_from_error_text() -> None:
    body = json.dumps({"message": f"bad key {API_KEY}"}).encode("utf-8")
    transport = _FakeTransport([(403, body)])

    with pytest.raises(MistralAuthError) as excinfo:
        mistral_json_request(
            method="GET",
            path=MISTRAL_MODELS_PATH,
            api_key=API_KEY,
            transport=transport,
            sleep=_no_sleep,
        )

    assert API_KEY not in str(excinfo.value)
    assert "[redacted]" in str(excinfo.value)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (402, b'{"message": "payment required"}'),
        (429, b'{"message": "insufficient credits for this workspace"}'),
        (429, b'{"message": "monthly quota exceeded"}'),
    ],
)
def test_mistral_json_request_classifies_quota_failures_without_retry(
    status: int, body: bytes
) -> None:
    transport = _FakeTransport([(status, body)])

    with pytest.raises(MistralQuotaError, match="quota/funds"):
        mistral_json_request(
            method="GET",
            path=MISTRAL_MODELS_PATH,
            api_key=API_KEY,
            transport=transport,
            sleep=_no_sleep,
        )

    assert len(transport.calls) == 1


def test_mistral_json_request_retries_transient_failures_then_unavailable() -> None:
    transport = _FakeTransport(
        [
            (429, b'{"message": "requests rate limit exceeded"}'),
            (503, b"upstream unavailable"),
            (500, b"server error"),
        ]
    )
    slept: list[float] = []

    with pytest.raises(MistralUnavailableError, match="after 3 attempts"):
        mistral_json_request(
            method="GET",
            path=MISTRAL_MODELS_PATH,
            api_key=API_KEY,
            transport=transport,
            sleep=slept.append,
        )

    assert len(transport.calls) == 3
    assert slept == [0.5, 1.0]


def test_mistral_json_request_recovers_after_one_transient_failure() -> None:
    transport = _FakeTransport(
        [
            OSError("connection reset"),
            (200, b'{"ok": true}'),
        ]
    )

    envelope = mistral_json_request(
        method="GET",
        path=MISTRAL_MODELS_PATH,
        api_key=API_KEY,
        transport=transport,
        sleep=_no_sleep,
    )

    assert envelope == {"ok": True}
    assert len(transport.calls) == 2


def test_mistral_json_request_treats_connection_failures_as_unavailable() -> None:
    transport = _FakeTransport([OSError("no route"), OSError("no route"), OSError("no route")])

    with pytest.raises(MistralUnavailableError, match="no route"):
        mistral_json_request(
            method="GET",
            path=MISTRAL_MODELS_PATH,
            api_key=API_KEY,
            transport=transport,
            sleep=_no_sleep,
        )


@pytest.mark.parametrize(
    ("status", "body", "match"),
    [
        (422, b'{"message": "validation error"}', "HTTP 422"),
        (200, b"not-json", "was not JSON"),
        (200, b"[1, 2]", "must be a JSON object"),
    ],
)
def test_mistral_json_request_rejects_contract_violations(
    status: int, body: bytes, match: str
) -> None:
    transport = _FakeTransport([(status, body)])

    with pytest.raises(MistralContractError, match=match):
        mistral_json_request(
            method="GET",
            path=MISTRAL_MODELS_PATH,
            api_key=API_KEY,
            transport=transport,
            sleep=_no_sleep,
        )


@pytest.mark.parametrize(
    "entry",
    [
        {"id": "mistral-small-2603", "aliases": [MISTRAL_SUMMARY_REQUEST_MODEL]},
        {"id": MISTRAL_SUMMARY_REQUEST_MODEL, "aliases": ["mistral-small-2603"]},
    ],
)
def test_resolve_mistral_summary_model_accepts_the_recorded_snapshot(
    entry: dict[str, object],
) -> None:
    transport = _FakeTransport([(200, _models_body(entry))])

    resolution = resolve_mistral_summary_model(
        api_key=API_KEY,
        transport=transport,
        sleep=_no_sleep,
    )

    assert resolution.request_model == MISTRAL_SUMMARY_REQUEST_MODEL
    assert resolution.resolved_model == "mistral-small-2603"
    assert resolution.allowed is True
    assert resolution.policy == MISTRAL_SUMMARY_ALLOWLIST_POLICY
    assert MISTRAL_SUMMARY_ALLOWLIST_POLICY in resolution.detail


def test_resolve_mistral_summary_model_fails_closed_on_alias_drift() -> None:
    transport = _FakeTransport(
        [
            (
                200,
                _models_body(
                    {"id": "mistral-small-2707", "aliases": [MISTRAL_SUMMARY_REQUEST_MODEL]}
                ),
            )
        ]
    )

    resolution = resolve_mistral_summary_model(
        api_key=API_KEY,
        transport=transport,
        sleep=_no_sleep,
    )

    assert resolution.resolved_model == "mistral-small-2707"
    assert resolution.allowed is False
    assert "failing closed" in resolution.detail
    assert MISTRAL_SUMMARY_ALLOWLIST_POLICY in resolution.detail


def test_resolve_mistral_summary_model_fails_closed_when_request_model_is_missing() -> None:
    transport = _FakeTransport(
        [(200, _models_body({"id": "mistral-large-2512", "aliases": ["mistral-large-latest"]}))]
    )

    resolution = resolve_mistral_summary_model(
        api_key=API_KEY,
        transport=transport,
        sleep=_no_sleep,
    )

    assert resolution.resolved_model is None
    assert resolution.allowed is False
    assert "was not found" in resolution.detail


def test_resolve_mistral_summary_model_rejects_invalid_models_envelope() -> None:
    transport = _FakeTransport([(200, b'{"data": "nope"}')])

    with pytest.raises(MistralContractError, match="data array"):
        resolve_mistral_summary_model(
            api_key=API_KEY,
            transport=transport,
            sleep=_no_sleep,
        )


def test_check_mistral_summary_readiness_requires_the_mena_scoped_key() -> None:
    transport = _FakeTransport([])

    state = check_mistral_summary_readiness(
        env={GENERIC_MISTRAL_API_KEY_ENV: "generic-key"},
        transport=transport,
        sleep=_no_sleep,
    )

    assert state.key_present is False
    assert state.model_allowed is False
    assert MENA_MISTRAL_API_KEY_ENV in state.detail
    assert GENERIC_MISTRAL_API_KEY_ENV in state.detail
    assert transport.calls == []


def test_check_mistral_summary_readiness_records_resolved_model_evidence() -> None:
    transport = _FakeTransport(
        [(200, _models_body({"id": "mistral-small-2603", "aliases": ["mistral-small-latest"]}))]
    )

    state = check_mistral_summary_readiness(
        env={MENA_MISTRAL_API_KEY_ENV: API_KEY},
        transport=transport,
        sleep=_no_sleep,
    )

    assert state.key_present is True
    assert state.reachable is True
    assert state.resolved_model == "mistral-small-2603"
    assert state.model_allowed is True
    assert API_KEY not in json.dumps(state.to_dict())


def test_check_mistral_summary_readiness_reports_invalid_key() -> None:
    transport = _FakeTransport([(401, b'{"message": "Unauthorized"}')])

    state = check_mistral_summary_readiness(
        env={MENA_MISTRAL_API_KEY_ENV: API_KEY},
        transport=transport,
        sleep=_no_sleep,
    )

    assert state.key_present is True
    assert state.reachable is True
    assert state.model_allowed is False
    assert "rejected by Mistral" in state.detail
    assert API_KEY not in state.detail


def test_check_mistral_summary_readiness_reports_quota_problems() -> None:
    transport = _FakeTransport([(402, b'{"message": "insufficient funds"}')])

    state = check_mistral_summary_readiness(
        env={MENA_MISTRAL_API_KEY_ENV: API_KEY},
        transport=transport,
        sleep=_no_sleep,
    )

    assert state.reachable is True
    assert state.model_allowed is False
    assert "quota/funds" in state.detail


def test_check_mistral_summary_readiness_reports_unreachable_provider() -> None:
    transport = _FakeTransport([OSError("dns"), OSError("dns"), OSError("dns")])

    state = check_mistral_summary_readiness(
        env={MENA_MISTRAL_API_KEY_ENV: API_KEY},
        transport=transport,
        sleep=_no_sleep,
    )

    assert state.key_present is True
    assert state.reachable is False
    assert "readiness probe failed" in state.detail


def test_mistral_chat_completion_sends_strict_json_schema_response_format() -> None:
    transport = _FakeTransport([(200, b'{"ok": true}')])
    schema = {"type": "object", "required": ["x"], "properties": {"x": {"type": "string"}}}

    mistral_chat_completion(
        api_key=API_KEY,
        model=MISTRAL_SUMMARY_REQUEST_MODEL,
        messages=({"role": "system", "content": "s"}, {"role": "user", "content": "u"}),
        schema=schema,
        schema_name="mena_test",
        temperature=0,
        random_seed=0,
        transport=transport,
        sleep=_no_sleep,
    )

    method, url, _headers, body = transport.calls[0]
    assert method == "POST"
    assert url == f"{MISTRAL_API_BASE_URL}{MISTRAL_CHAT_COMPLETIONS_PATH}"
    assert body is not None
    payload = json.loads(body)
    assert payload["model"] == MISTRAL_SUMMARY_REQUEST_MODEL
    assert payload["temperature"] == 0
    assert payload["random_seed"] == 0
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "mena_test", "schema": schema, "strict": True},
    }


def test_mistral_chat_response_helpers_extract_the_first_choice() -> None:
    response = {
        "model": "mistral-small-2603",
        "choices": [
            {"finish_reason": "stop", "message": {"content": '{"x": "y"}'}},
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
    }

    assert mistral_message_content(response) == '{"x": "y"}'
    assert mistral_finish_reason(response) == "stop"
    assert mistral_token_counts(response) == (10, 3)
    assert mistral_response_model(response) == "mistral-small-2603"
    assert mistral_finish_reason({"choices": [{"message": {"content": "x"}}]}) is None


@pytest.mark.parametrize(
    ("response", "match"),
    [
        ({}, "missing choices"),
        ({"choices": []}, "missing choices"),
        ({"choices": ["nope"]}, "must be a JSON object"),
        ({"choices": [{}]}, "missing a message object"),
        ({"choices": [{"message": {"content": "  "}}]}, "content is empty"),
        ({"choices": [{"finish_reason": 5, "message": {}}]}, "finish_reason is invalid"),
    ],
)
def test_mistral_chat_response_helpers_reject_contract_violations(
    response: dict[str, object], match: str
) -> None:
    with pytest.raises(MistralContractError, match=match):
        if "finish_reason" in str(response):
            mistral_finish_reason(response)
        else:
            mistral_message_content(response)


def test_mistral_token_counts_require_a_usage_object() -> None:
    with pytest.raises(MistralContractError, match="usage object"):
        mistral_token_counts({"choices": [{"message": {"content": "x"}}]})
    with pytest.raises(MistralContractError, match="prompt_tokens"):
        mistral_token_counts({"usage": {"prompt_tokens": -1, "completion_tokens": 2}})
    with pytest.raises(MistralContractError, match="completion_tokens"):
        mistral_token_counts({"usage": {"prompt_tokens": 1, "completion_tokens": True}})


def test_mistral_response_model_requires_model_identity() -> None:
    with pytest.raises(MistralContractError, match="model identity"):
        mistral_response_model({"choices": []})
