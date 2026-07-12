"""Shared strict readers for JSON-like fields."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

_TError = TypeVar("_TError", bound=Exception)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ContractReader(Generic[_TError]):
    context_label: str
    error: type[_TError]

    def required_str(self, data: Mapping[str, object], key: str) -> str:
        return required_str(
            data,
            key,
            error=self.error,
            message=self._message(key, "is required and must be a non-empty string"),
        )

    def optional_str(self, data: Mapping[str, object], key: str) -> str | None:
        return optional_str(
            data,
            key,
            error=self.error,
            message=self._message(key, "must be a non-empty string or null"),
        )

    def required_nullable_str(self, data: Mapping[str, object], key: str) -> str | None:
        return required_nullable_str(
            data,
            key,
            error=self.error,
            missing_message=self._message(key, "is required"),
            invalid_message=self._message(key, "must be a non-empty string or null"),
        )

    def required_int(self, data: Mapping[str, object], key: str) -> int:
        return required_int(
            data,
            key,
            error=self.error,
            message=self._message(key, "must be an integer"),
        )

    def optional_int(self, data: Mapping[str, object], key: str) -> int | None:
        return optional_int(
            data,
            key,
            error=self.error,
            message=self._message(key, "must be an integer or null"),
        )

    def required_bool(self, data: Mapping[str, object], key: str) -> bool:
        return required_bool(
            data,
            key,
            error=self.error,
            message=self._message(key, "must be a boolean"),
        )

    def required_number(self, data: Mapping[str, object], key: str) -> float:
        return required_number(
            data,
            key,
            error=self.error,
            message=self._message(key, "must be a number"),
        )

    def optional_number(self, data: Mapping[str, object], key: str) -> float | None:
        return optional_number(
            data,
            key,
            error=self.error,
            message=self._message(key, "must be a number or null"),
        )

    def required_mapping(
        self,
        data: Mapping[str, object],
        key: str,
    ) -> Mapping[str, object]:
        return required_mapping(
            data,
            key,
            error=self.error,
            message=self._message(key, "must be a JSON object"),
        )

    def required_dict(self, data: Mapping[str, object], key: str) -> dict[str, object]:
        return required_dict(
            data,
            key,
            error=self.error,
            message=self._message(key, "must be a JSON object"),
        )

    def optional_mapping(
        self,
        data: Mapping[str, object],
        key: str,
    ) -> Mapping[str, object]:
        return optional_mapping(
            data,
            key,
            error=self.error,
            message=self._message(key, "must be a JSON object or null"),
        )

    def required_list(self, data: Mapping[str, object], key: str) -> list[object]:
        return required_list(
            data,
            key,
            error=self.error,
            message=self._message(key, "must be an array"),
        )

    def _message(self, key: str, requirement: str) -> str:
        return f"{self.context_label} field '{key}' {requirement}."


def required_str(
    data: Mapping[str, object], key: str, *, error: type[_TError], message: str
) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise error(message)
    return value


def optional_str(
    data: Mapping[str, object],
    key: str,
    *,
    error: type[_TError],
    message: str,
) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise error(message)
    return value


def required_nullable_str(
    data: Mapping[str, object],
    key: str,
    *,
    error: type[_TError],
    missing_message: str,
    invalid_message: str,
) -> str | None:
    if key not in data:
        raise error(missing_message)
    value = data[key]
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise error(invalid_message)
    return value


def required_int(
    data: Mapping[str, object], key: str, *, error: type[_TError], message: str
) -> int:
    value = data.get(key)
    if type(value) is not int:
        raise error(message)
    return value


def optional_int(
    data: Mapping[str, object],
    key: str,
    *,
    error: type[_TError],
    message: str,
) -> int | None:
    value = data.get(key)
    if value is None:
        return None
    if type(value) is not int:
        raise error(message)
    return value


def required_bool(
    data: Mapping[str, object],
    key: str,
    *,
    error: type[_TError],
    message: str,
) -> bool:
    value = data.get(key)
    if type(value) is not bool:
        raise error(message)
    return value


def required_number(
    data: Mapping[str, object],
    key: str,
    *,
    error: type[_TError],
    message: str,
) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise error(message)
    return float(value)


def optional_number(
    data: Mapping[str, object],
    key: str,
    *,
    error: type[_TError],
    message: str,
) -> float | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise error(message)
    return float(value)


def required_mapping(
    data: Mapping[str, object],
    key: str,
    *,
    error: type[_TError],
    message: str,
) -> Mapping[str, object]:
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise error(message)
    return value


def required_dict(
    data: Mapping[str, object],
    key: str,
    *,
    error: type[_TError],
    message: str,
) -> dict[str, object]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise error(message)
    return value


def optional_mapping(
    data: Mapping[str, object],
    key: str,
    *,
    error: type[_TError],
    message: str,
) -> Mapping[str, object]:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise error(message)
    return value


def required_list(
    data: Mapping[str, object],
    key: str,
    *,
    error: type[_TError],
    message: str,
) -> list[object]:
    value = data.get(key)
    if not isinstance(value, list):
        raise error(message)
    return value


def required_enum_str(
    data: Mapping[str, object],
    key: str,
    allowed_values: frozenset[str],
    *,
    error: type[_TError],
    string_message: str,
    enum_message: str,
) -> str:
    value = required_str(data, key, error=error, message=string_message)
    if value not in allowed_values:
        raise error(enum_message)
    return value


def optional_enum_str(
    data: Mapping[str, object],
    key: str,
    allowed_values: frozenset[str],
    *,
    error: type[_TError],
    string_message: str,
    enum_message: str,
) -> str | None:
    value = optional_str(data, key, error=error, message=string_message)
    if value is None:
        return None
    if value not in allowed_values:
        raise error(enum_message)
    return value


def required_sha256(
    data: Mapping[str, object],
    key: str,
    *,
    error: type[_TError],
    string_message: str,
    sha_message: str,
) -> str:
    value = required_str(data, key, error=error, message=string_message)
    if not _SHA256_PATTERN.fullmatch(value):
        raise error(sha_message)
    return value


def require_exact_keys(
    data: Mapping[str, object],
    expected_keys: Sequence[str],
    *,
    error: type[_TError],
    message: str,
) -> None:
    if set(data) != set(expected_keys):
        raise error(message)
