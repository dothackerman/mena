"""Mena-scoped direct Mistral API adapter helpers for remote summarization.

Endpoint, request-model, and allow-list policy are provider/policy owned per
ADR-033: no user-configurable Mistral endpoints or model IDs. The API key comes
only from the Mena-scoped ``MENA_MISTRAL_API_KEY`` environment variable and must
never be persisted or echoed in state, config, logs, provenance, or errors.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256

MISTRAL_API_BASE_URL = "https://api.mistral.ai"
MISTRAL_CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
MISTRAL_MODELS_PATH = "/v1/models"
MENA_MISTRAL_API_KEY_ENV = "MENA_MISTRAL_API_KEY"
# Deliberately never consumed (issue #29): a generic key on the host must not
# silently opt a Mena session into remote summarization.
GENERIC_MISTRAL_API_KEY_ENV = "MISTRAL_API_KEY"

# Fixed PoC model selection from the #92 spike. `mistral-small-latest` is used
# as request ID only while it resolves inside the recorded Mistral Small 4
# snapshot; alias drift outside the allow-list must fail closed.
MISTRAL_SUMMARY_REQUEST_MODEL = "mistral-small-latest"
MISTRAL_SUMMARY_ALLOWLIST_POLICY = "mena-mistral-summary-allowlist-2026-06-29-v1"
MISTRAL_SUMMARY_EXPECTED_SNAPSHOT = "mistral-small-2603+1"
# The model card labels the accepted Small 4 snapshot `mistral-small-2603+1`;
# the API may report the base dated ID without the card revision suffix.
MISTRAL_SUMMARY_ALLOWED_RESOLVED_MODELS = frozenset({"mistral-small-2603", "mistral-small-2603+1"})

MISTRAL_REQUEST_TIMEOUT_SECONDS = 60.0
MISTRAL_MAX_TRANSIENT_RETRIES = 2
_RETRY_BACKOFF_BASE_SECONDS = 0.5
_ERROR_BODY_EXCERPT_LIMIT = 300
_QUOTA_BODY_MARKERS = ("quota", "credit", "billing", "insufficient", "funds", "balance")

# Minimum PDR-004 implementation disclosure shown before Mistral may be
# accepted as summarization primary or fallback. Domain data, not adapter
# wording: interface adapters render these lines verbatim or restyle them.
# The summarization text explicitly includes speaker labels: the shared
# provider-independent request carries per-segment speaker attribution, and
# corrected labels can be real names, so the disclosure must cover them
# rather than claim all speaker data stays local.
MISTRAL_SUMMARY_DISCLOSURE = (
    "Transcript text selected for summarization is sent to your configured "
    "Mistral account, including per-segment speaker labels (which may be "
    "real names after correction) and manual notes when included.",
    "Audio, ASR and diarization processing, raw local artifacts, speaker "
    "embeddings, and other speaker-sensitive artifacts stay local; nothing "
    "beyond the summarization text above is uploaded.",
    "Mistral use may incur API charges. Account, quota, subscription, ZDR "
    "activation, and legal posture are your responsibility; Mena cannot "
    "verify them.",
    "ZDR does not remove all residuals: Mistral may retain non-content "
    "account, support, billing, abuse-prevention, or operational metadata, "
    "and Mistral-controlled routing or terms may still affect jurisdictional "
    "risk.",
    "Do not use remote summarization for meetings likely to contain "
    "special-category or similarly sensitive personal data unless you have "
    "confirmed that your Mistral account, legal basis, and contractual "
    "posture are appropriate for that content.",
    "Artifacts and runtime provenance record when Mistral was used.",
)


class MistralAPIError(RuntimeError):
    """Base class for direct Mistral API failures."""


class MistralAuthError(MistralAPIError):
    """The Mena-scoped API key is missing, invalid, or not authorized."""


class MistralQuotaError(MistralAPIError):
    """Mistral reported quota/funds exhaustion for the configured account."""


class MistralUnavailableError(MistralAPIError):
    """Mistral could not be reached or stayed transiently failing after retries."""


class MistralContractError(MistralAPIError):
    """A Mistral request or response violated the adapter contract."""


Transport = Callable[[str, str, Mapping[str, str], bytes | None, float], tuple[int, bytes]]
SleepFn = Callable[[float], None]


@dataclass(frozen=True)
class MistralModelResolution:
    """Result of resolving the fixed request model against ``/v1/models``."""

    request_model: str
    resolved_model: str | None
    allowed: bool
    policy: str
    detail: str


@dataclass(frozen=True)
class MistralSummaryState:
    """Detected Mistral summarization readiness. Never contains key material."""

    key_present: bool
    reachable: bool
    request_model: str
    resolved_model: str | None
    model_allowed: bool
    policy: str
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "key_present": self.key_present,
            "reachable": self.reachable,
            "request_model": self.request_model,
            "resolved_model": self.resolved_model,
            "model_allowed": self.model_allowed,
            "policy": self.policy,
            "detail": self.detail,
        }


def load_mena_mistral_api_key(env: Mapping[str, str]) -> str | None:
    """Return the Mena-scoped Mistral API key, never the generic provider key."""

    key = env.get(MENA_MISTRAL_API_KEY_ENV)
    if key is None or not key.strip():
        return None
    return key.strip()


def mistral_account_scope(api_key: str) -> str:
    """Return a non-secret account identity for summary reuse scoping (ADR-033).

    ADR-033 requires summary reuse to be scoped by non-secret Mistral
    account/workspace identity when remote account posture affects
    authorization. Mena cannot observe a workspace ID without extra API
    surface, so the key itself is the account posture signal: a truncated,
    domain-separated SHA-256 fingerprint changes exactly when the configured
    key changes, is not reversible, and never exposes key material.
    """

    digest = sha256(f"mena-mistral-account-scope:{api_key}".encode()).hexdigest()
    return f"mistral-account-{digest[:16]}"


def _urllib_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout_seconds: float,
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


def mistral_json_request(
    *,
    method: str,
    path: str,
    api_key: str,
    payload: Mapping[str, object] | None = None,
    timeout_seconds: float = MISTRAL_REQUEST_TIMEOUT_SECONDS,
    transport: Transport | None = None,
    sleep: SleepFn = time.sleep,
) -> Mapping[str, object]:
    """Call the fixed direct Mistral API and return one JSON object envelope.

    Transient failures (connection errors, 408/429 rate limiting, 5xx) are
    retried a bounded number of times, then surfaced as provider unavailable.
    Authentication and quota/funds failures are never retried.
    """

    send = transport or _urllib_transport
    url = f"{MISTRAL_API_BASE_URL}{path}"
    headers = {
        "authorization": f"Bearer {api_key}",
        "content-type": "application/json",
        "accept": "application/json",
    }
    body = None if payload is None else json.dumps(payload).encode("utf-8")

    last_transient: str | None = None
    for attempt in range(1 + MISTRAL_MAX_TRANSIENT_RETRIES):
        if attempt:
            sleep(_RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        try:
            status, raw = send(method, url, headers, body, timeout_seconds)
        except OSError as exc:
            last_transient = _redact(str(exc) or exc.__class__.__name__, api_key)
            continue
        if status in {408, 429} or status >= 500:
            classified = _classify_http_failure(status, raw, api_key)
            if isinstance(classified, MistralQuotaError):
                raise classified
            last_transient = str(classified)
            continue
        if status >= 400:
            raise _classify_http_failure(status, raw, api_key)
        return _decode_json_object(
            raw,
            api_key,
            allow_models_array=path == MISTRAL_MODELS_PATH,
        )
    raise MistralUnavailableError(
        "Mistral API is unavailable after "
        f"{1 + MISTRAL_MAX_TRANSIENT_RETRIES} attempts: {last_transient}"
    )


def resolve_mistral_summary_model(
    *,
    api_key: str,
    request_model: str = MISTRAL_SUMMARY_REQUEST_MODEL,
    timeout_seconds: float = MISTRAL_REQUEST_TIMEOUT_SECONDS,
    transport: Transport | None = None,
    sleep: SleepFn = time.sleep,
) -> MistralModelResolution:
    """Resolve the fixed request model through ``/v1/models`` and allow-list it."""

    envelope = mistral_json_request(
        method="GET",
        path=MISTRAL_MODELS_PATH,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        transport=transport,
        sleep=sleep,
    )
    data = envelope.get("data")
    if not isinstance(data, list):
        raise MistralContractError("Mistral models response must contain a data array.")
    resolved = _resolved_model_id(data, request_model)
    if resolved is None:
        return MistralModelResolution(
            request_model=request_model,
            resolved_model=None,
            allowed=False,
            policy=MISTRAL_SUMMARY_ALLOWLIST_POLICY,
            detail=(
                f"Request model {request_model!r} was not found in the Mistral model "
                f"list; refusing under {MISTRAL_SUMMARY_ALLOWLIST_POLICY}."
            ),
        )
    if resolved not in MISTRAL_SUMMARY_ALLOWED_RESOLVED_MODELS:
        return MistralModelResolution(
            request_model=request_model,
            resolved_model=resolved,
            allowed=False,
            policy=MISTRAL_SUMMARY_ALLOWLIST_POLICY,
            detail=(
                f"Request model {request_model!r} resolved to {resolved!r}, outside the "
                f"accepted Mistral Small 4 snapshot {MISTRAL_SUMMARY_EXPECTED_SNAPSHOT!r}; "
                f"failing closed until a new allow-list review updates "
                f"{MISTRAL_SUMMARY_ALLOWLIST_POLICY}."
            ),
        )
    return MistralModelResolution(
        request_model=request_model,
        resolved_model=resolved,
        allowed=True,
        policy=MISTRAL_SUMMARY_ALLOWLIST_POLICY,
        detail=(
            f"Request model {request_model!r} resolved to {resolved!r} inside the "
            f"accepted snapshot under {MISTRAL_SUMMARY_ALLOWLIST_POLICY}."
        ),
    )


def check_mistral_summary_readiness(
    *,
    env: Mapping[str, str],
    timeout_seconds: float = MISTRAL_REQUEST_TIMEOUT_SECONDS,
    transport: Transport | None = None,
    sleep: SleepFn = time.sleep,
) -> MistralSummaryState:
    """Probe Mistral summarization readiness: key presence plus model resolution.

    The probe is the narrowest useful readiness check the current API supports:
    an authenticated ``/v1/models`` call proves the key works without sending
    any meeting content, and doubles as the allow-list resolution evidence.
    """

    api_key = load_mena_mistral_api_key(env)
    if api_key is None:
        return MistralSummaryState(
            key_present=False,
            reachable=False,
            request_model=MISTRAL_SUMMARY_REQUEST_MODEL,
            resolved_model=None,
            model_allowed=False,
            policy=MISTRAL_SUMMARY_ALLOWLIST_POLICY,
            detail=(
                f"{MENA_MISTRAL_API_KEY_ENV} is not set. Generic "
                f"{GENERIC_MISTRAL_API_KEY_ENV} is intentionally not used."
            ),
        )
    try:
        resolution = resolve_mistral_summary_model(
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            transport=transport,
            sleep=sleep,
        )
    except MistralAuthError as exc:
        return MistralSummaryState(
            key_present=True,
            reachable=True,
            request_model=MISTRAL_SUMMARY_REQUEST_MODEL,
            resolved_model=None,
            model_allowed=False,
            policy=MISTRAL_SUMMARY_ALLOWLIST_POLICY,
            detail=f"{MENA_MISTRAL_API_KEY_ENV} was rejected by Mistral: {exc}",
        )
    except MistralQuotaError as exc:
        return MistralSummaryState(
            key_present=True,
            reachable=True,
            request_model=MISTRAL_SUMMARY_REQUEST_MODEL,
            resolved_model=None,
            model_allowed=False,
            policy=MISTRAL_SUMMARY_ALLOWLIST_POLICY,
            detail=f"Mistral reported a quota/funds problem: {exc}",
        )
    except (MistralUnavailableError, MistralContractError) as exc:
        return MistralSummaryState(
            key_present=True,
            reachable=False,
            request_model=MISTRAL_SUMMARY_REQUEST_MODEL,
            resolved_model=None,
            model_allowed=False,
            policy=MISTRAL_SUMMARY_ALLOWLIST_POLICY,
            detail=f"Mistral readiness probe failed: {exc}",
        )
    return MistralSummaryState(
        key_present=True,
        reachable=True,
        request_model=resolution.request_model,
        resolved_model=resolution.resolved_model,
        model_allowed=resolution.allowed,
        policy=resolution.policy,
        detail=resolution.detail,
    )


def mistral_chat_completion(
    *,
    api_key: str,
    model: str,
    messages: Sequence[Mapping[str, str]],
    schema: Mapping[str, object],
    schema_name: str,
    temperature: float,
    random_seed: int,
    timeout_seconds: float = MISTRAL_REQUEST_TIMEOUT_SECONDS,
    transport: Transport | None = None,
    sleep: SleepFn = time.sleep,
) -> Mapping[str, object]:
    """Run one structured-output chat completion against the fixed endpoint.

    Uses Mistral's strongest supported structured-output mechanism for a fixed
    JSON schema: ``response_format`` with ``type: json_schema`` and
    ``strict: true``.
    """

    payload: dict[str, object] = {
        "model": model,
        "temperature": temperature,
        "random_seed": random_seed,
        "messages": list(messages),
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "schema": dict(schema),
                "strict": True,
            },
        },
    }
    return mistral_json_request(
        method="POST",
        path=MISTRAL_CHAT_COMPLETIONS_PATH,
        api_key=api_key,
        payload=payload,
        timeout_seconds=timeout_seconds,
        transport=transport,
        sleep=sleep,
    )


def mistral_message_content(response: Mapping[str, object]) -> str:
    """Extract assistant message content from a chat completion envelope."""

    message = _first_choice(response).get("message")
    if not isinstance(message, Mapping):
        raise MistralContractError("Mistral response choice is missing a message object.")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise MistralContractError("Mistral response message content is empty.")
    return content


def mistral_finish_reason(response: Mapping[str, object]) -> str | None:
    """Return the first choice finish reason when present."""

    reason = _first_choice(response).get("finish_reason")
    if reason is None:
        return None
    if not isinstance(reason, str):
        raise MistralContractError("Mistral response finish_reason is invalid.")
    return reason


def mistral_token_counts(response: Mapping[str, object]) -> tuple[int, int]:
    """Return (prompt_tokens, completion_tokens) from the usage block."""

    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        raise MistralContractError("Mistral response is missing a usage object.")
    return (
        _required_token_count(usage, "prompt_tokens"),
        _required_token_count(usage, "completion_tokens"),
    )


def mistral_response_model(response: Mapping[str, object]) -> str:
    """Return the model identity echoed by a chat completion response."""

    model = response.get("model")
    if not isinstance(model, str) or not model:
        raise MistralContractError("Mistral response is missing its model identity.")
    return model


def _first_choice(response: Mapping[str, object]) -> Mapping[str, object]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise MistralContractError("Mistral response is missing choices.")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise MistralContractError("Mistral response choice must be a JSON object.")
    return first


def _required_token_count(usage: Mapping[str, object], field_name: str) -> int:
    value = usage.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MistralContractError(
            f"Mistral usage field {field_name!r} must be a non-negative integer."
        )
    return value


def _resolved_model_id(models: list[object], request_model: str) -> str | None:
    """Return the canonical dated ID the request model currently points at."""

    for entry in models:
        if not isinstance(entry, Mapping):
            continue
        entry_id = entry.get("id")
        if not isinstance(entry_id, str):
            continue
        aliases = entry.get("aliases")
        alias_values = (
            tuple(alias for alias in aliases if isinstance(alias, str))
            if isinstance(aliases, list)
            else ()
        )
        if entry_id != request_model and request_model in alias_values:
            return entry_id
        if entry_id == request_model:
            for alias in alias_values:
                if alias != request_model:
                    return alias
    return None


def _classify_http_failure(status: int, raw: bytes, api_key: str) -> MistralAPIError:
    excerpt = _body_excerpt(raw, api_key)
    if status in {401, 403}:
        return MistralAuthError(
            f"Mistral rejected the configured API key (HTTP {status}). {excerpt}".strip()
        )
    if status == 402 or (status == 429 and _looks_like_quota(excerpt)):
        return MistralQuotaError(
            f"Mistral reported quota/funds exhaustion (HTTP {status}). {excerpt}".strip()
        )
    if status in {408, 429} or status >= 500:
        return MistralUnavailableError(
            f"Mistral transient failure (HTTP {status}). {excerpt}".strip()
        )
    return MistralContractError(f"Mistral request failed (HTTP {status}). {excerpt}".strip())


def _looks_like_quota(excerpt: str) -> bool:
    lowered = excerpt.lower()
    return any(marker in lowered for marker in _QUOTA_BODY_MARKERS)


def _decode_json_object(
    raw: bytes,
    api_key: str,
    *,
    allow_models_array: bool = False,
) -> Mapping[str, object]:
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MistralContractError("Mistral response envelope was not JSON.") from exc
    if allow_models_array and isinstance(decoded, list):
        return {"data": decoded}
    if not isinstance(decoded, Mapping):
        raise MistralContractError("Mistral response envelope must be a JSON object.")
    _ = api_key
    return decoded


def _body_excerpt(raw: bytes, api_key: str) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    text = _redact(text, api_key)
    if len(text) > _ERROR_BODY_EXCERPT_LIMIT:
        text = f"{text[:_ERROR_BODY_EXCERPT_LIMIT]}... [truncated]"
    return text


def _redact(text: str, api_key: str) -> str:
    if api_key and api_key in text:
        return text.replace(api_key, "[redacted]")
    return text
