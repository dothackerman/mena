from collections.abc import Mapping
from types import MappingProxyType

import pytest

from mena.fields import (
    optional_enum_str,
    optional_mapping,
    optional_number,
    optional_str,
    require_exact_keys,
    required_bool,
    required_dict,
    required_enum_str,
    required_int,
    required_list,
    required_mapping,
    required_nullable_str,
    required_number,
    required_sha256,
    required_str,
)


class FieldError(ValueError):
    pass


def test_required_str_rejects_missing_empty_and_non_string_values() -> None:
    assert required_str({"name": "value"}, "name", error=FieldError, message="bad") == "value"

    for payload in ({}, {"name": ""}, {"name": 1}):
        with pytest.raises(FieldError, match="bad"):
            required_str(payload, "name", error=FieldError, message="bad")


def test_optional_str_accepts_missing_and_null_but_rejects_bad_values() -> None:
    assert optional_str({}, "name", error=FieldError, message="bad") is None
    assert optional_str({"name": None}, "name", error=FieldError, message="bad") is None
    assert optional_str({"name": "value"}, "name", error=FieldError, message="bad") == "value"

    with pytest.raises(FieldError, match="bad"):
        optional_str({"name": ""}, "name", error=FieldError, message="bad")


def test_required_nullable_str_distinguishes_missing_from_null() -> None:
    assert (
        required_nullable_str(
            {"name": None},
            "name",
            error=FieldError,
            missing_message="missing",
            invalid_message="bad",
        )
        is None
    )
    assert (
        required_nullable_str(
            {"name": "value"},
            "name",
            error=FieldError,
            missing_message="missing",
            invalid_message="bad",
        )
        == "value"
    )

    with pytest.raises(FieldError, match="missing"):
        required_nullable_str(
            {},
            "name",
            error=FieldError,
            missing_message="missing",
            invalid_message="bad",
        )
    with pytest.raises(FieldError, match="bad"):
        required_nullable_str(
            {"name": 1},
            "name",
            error=FieldError,
            missing_message="missing",
            invalid_message="bad",
        )


def test_required_int_rejects_bool() -> None:
    assert required_int({"count": 1}, "count", error=FieldError, message="bad") == 1

    with pytest.raises(FieldError, match="bad"):
        required_int({"count": True}, "count", error=FieldError, message="bad")


def test_required_bool_requires_bool_type() -> None:
    assert required_bool({"flag": False}, "flag", error=FieldError, message="bad") is False

    with pytest.raises(FieldError, match="bad"):
        required_bool({"flag": 0}, "flag", error=FieldError, message="bad")


def test_number_readers_reject_bool_and_return_float() -> None:
    assert required_number({"score": 1}, "score", error=FieldError, message="bad") == 1.0
    assert optional_number({}, "score", error=FieldError, message="bad") is None
    assert optional_number({"score": 1.5}, "score", error=FieldError, message="bad") == 1.5

    with pytest.raises(FieldError, match="bad"):
        required_number({"score": True}, "score", error=FieldError, message="bad")
    with pytest.raises(FieldError, match="bad"):
        optional_number({"score": "high"}, "score", error=FieldError, message="bad")


def test_mapping_and_list_readers_are_strict() -> None:
    mapping = required_mapping({"item": {"a": 1}}, "item", error=FieldError, message="bad")
    assert isinstance(mapping, Mapping)
    assert optional_mapping({}, "item", error=FieldError, message="bad") == {}
    assert required_list({"items": [1]}, "items", error=FieldError, message="bad") == [1]

    with pytest.raises(FieldError, match="bad"):
        required_mapping({"item": []}, "item", error=FieldError, message="bad")
    with pytest.raises(FieldError, match="bad"):
        optional_mapping({"item": []}, "item", error=FieldError, message="bad")
    with pytest.raises(FieldError, match="bad"):
        required_list({"items": {}}, "items", error=FieldError, message="bad")


def test_required_dict_rejects_non_dict_mappings() -> None:
    assert required_dict({"item": {"a": 1}}, "item", error=FieldError, message="bad") == {"a": 1}

    with pytest.raises(FieldError, match="bad"):
        required_dict(
            {"item": MappingProxyType({"a": 1})},
            "item",
            error=FieldError,
            message="bad",
        )


def test_enum_readers_check_string_and_allowed_values() -> None:
    allowed = frozenset({"ready", "failed"})
    assert (
        required_enum_str(
            {"status": "ready"},
            "status",
            allowed,
            error=FieldError,
            string_message="string",
            enum_message="enum",
        )
        == "ready"
    )
    assert (
        optional_enum_str(
            {},
            "status",
            allowed,
            error=FieldError,
            string_message="string",
            enum_message="enum",
        )
        is None
    )

    with pytest.raises(FieldError, match="string"):
        required_enum_str(
            {"status": ""},
            "status",
            allowed,
            error=FieldError,
            string_message="string",
            enum_message="enum",
        )
    with pytest.raises(FieldError, match="enum"):
        optional_enum_str(
            {"status": "unknown"},
            "status",
            allowed,
            error=FieldError,
            string_message="string",
            enum_message="enum",
        )


def test_required_sha256_checks_lowercase_hex_digest() -> None:
    digest = "a" * 64
    assert (
        required_sha256(
            {"sha": digest},
            "sha",
            error=FieldError,
            string_message="string",
            sha_message="sha",
        )
        == digest
    )

    with pytest.raises(FieldError, match="sha"):
        required_sha256(
            {"sha": "A" * 64},
            "sha",
            error=FieldError,
            string_message="string",
            sha_message="sha",
        )


def test_require_exact_keys_reports_mismatch() -> None:
    require_exact_keys({"a": 1}, ("a",), error=FieldError, message="bad")

    with pytest.raises(FieldError, match="bad"):
        require_exact_keys({"a": 1, "b": 2}, ("a",), error=FieldError, message="bad")
