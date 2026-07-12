from collections.abc import Callable
from types import MappingProxyType

import pytest

from mena.fields import ContractReader, require_exact_keys, required_sha256


class FieldError(ValueError):
    pass


def test_contract_reader_formats_uniform_field_messages() -> None:
    reader = ContractReader("Session", FieldError)

    assert reader.required_str({"name": "value"}, "name") == "value"

    with pytest.raises(
        FieldError,
        match="Session field 'name' is required and must be a non-empty string.",
    ):
        reader.required_str({}, "name")


def test_contract_reader_covers_string_variants() -> None:
    reader = ContractReader("Artifact", FieldError)

    assert reader.optional_str({}, "name") is None
    assert reader.optional_str({"name": None}, "name") is None
    assert reader.optional_str({"name": "value"}, "name") == "value"
    assert reader.required_nullable_str({"name": None}, "name") is None
    assert reader.required_nullable_str({"name": "value"}, "name") == "value"

    with pytest.raises(FieldError, match="Artifact field 'name' is required."):
        reader.required_nullable_str({}, "name")
    with pytest.raises(
        FieldError,
        match="Artifact field 'name' must be a non-empty string or null.",
    ):
        reader.required_nullable_str({"name": ""}, "name")
    with pytest.raises(
        FieldError,
        match="Artifact field 'name' must be a non-empty string or null.",
    ):
        reader.optional_str({"name": ""}, "name")


def test_contract_reader_covers_scalar_value_kinds() -> None:
    reader = ContractReader("Protocol", FieldError)

    assert reader.required_int({"count": 1}, "count") == 1
    assert reader.optional_int({}, "count") is None
    assert reader.optional_int({"count": 1}, "count") == 1
    assert reader.required_nullable_int({"count": None}, "count") is None
    assert reader.required_nullable_int({"count": 1}, "count") == 1
    assert reader.required_bool({"enabled": False}, "enabled") is False
    assert reader.optional_bool({}, "enabled") is None
    assert reader.optional_bool({"enabled": False}, "enabled") is False
    assert reader.required_nullable_bool({"enabled": None}, "enabled") is None
    assert reader.required_nullable_bool({"enabled": False}, "enabled") is False
    assert reader.required_number({"score": 1}, "score") == 1.0
    assert reader.optional_number({}, "score") is None
    assert reader.optional_number({"score": 1.5}, "score") == 1.5

    scalar_error_cases: tuple[tuple[Callable[[], object], str], ...] = (
        (
            lambda: reader.required_int({"count": True}, "count"),
            "Protocol field 'count' must be an integer.",
        ),
        (
            lambda: reader.optional_int({"count": True}, "count"),
            "Protocol field 'count' must be an integer or null.",
        ),
        (
            lambda: reader.required_nullable_int({}, "count"),
            "Protocol field 'count' is required.",
        ),
        (
            lambda: reader.required_nullable_int({"count": True}, "count"),
            "Protocol field 'count' must be an integer or null.",
        ),
        (
            lambda: reader.required_bool({"enabled": 0}, "enabled"),
            "Protocol field 'enabled' must be a boolean.",
        ),
        (
            lambda: reader.optional_bool({"enabled": 0}, "enabled"),
            "Protocol field 'enabled' must be a boolean or null.",
        ),
        (
            lambda: reader.required_nullable_bool({}, "enabled"),
            "Protocol field 'enabled' is required.",
        ),
        (
            lambda: reader.required_nullable_bool({"enabled": 0}, "enabled"),
            "Protocol field 'enabled' must be a boolean or null.",
        ),
        (
            lambda: reader.required_number({"score": True}, "score"),
            "Protocol field 'score' must be a number.",
        ),
        (
            lambda: reader.optional_number({"score": True}, "score"),
            "Protocol field 'score' must be a number or null.",
        ),
    )
    for call, match in scalar_error_cases:
        with pytest.raises(FieldError, match=match):
            call()


def test_contract_reader_covers_collection_value_kinds() -> None:
    reader = ContractReader("Payload", FieldError)

    assert reader.required_mapping({"item": {"a": 1}}, "item") == {"a": 1}
    assert reader.required_dict({"item": {"a": 1}}, "item") == {"a": 1}
    assert reader.optional_mapping({}, "item") == {}
    assert reader.required_list({"items": [1]}, "items") == [1]

    with pytest.raises(FieldError, match="Payload field 'item' must be a JSON object."):
        reader.required_mapping({"item": []}, "item")
    with pytest.raises(FieldError, match="Payload field 'item' must be a JSON object."):
        reader.required_dict({"item": MappingProxyType({"a": 1})}, "item")
    with pytest.raises(
        FieldError,
        match="Payload field 'item' must be a JSON object or null.",
    ):
        reader.optional_mapping({"item": []}, "item")
    with pytest.raises(FieldError, match="Payload field 'items' must be an array."):
        reader.required_list({"items": {}}, "items")


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
