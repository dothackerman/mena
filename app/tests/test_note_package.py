import ast
import json
from pathlib import Path

import pytest

from mena.note_package import (
    PRODUCT_RESPONSE_SCHEMA_NAMES,
    PRODUCT_RESPONSE_SCHEMAS,
    REQUIRED_NOTE_PRODUCTS,
    ProductRequestRecord,
    SummarizationContractError,
    SummarizationPolicy,
    SummaryActionItem,
    SummaryResult,
    SummaryTimelineEntry,
    assemble_summary_result,
    assert_summary_output_language,
    product_system_prompt,
    product_user_prompt,
    validate_product_map_payload,
    validate_product_payload,
)
from test_summarization import _summary_request, _summary_result


def test_note_package_module_has_no_provider_transport_imports() -> None:
    module_path = Path(__file__).parents[1] / "src/mena/note_package.py"
    tree = ast.parse(module_path.read_text())
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)
        elif isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)

    assert "mena.local_llm" not in imported_modules
    assert "mena.mistral_llm" not in imported_modules


def test_note_package_defines_ordered_summary_v2_product_contract() -> None:
    assert REQUIRED_NOTE_PRODUCTS == ("timeline", "action_items", "general_summary")
    assert tuple(PRODUCT_RESPONSE_SCHEMAS) == REQUIRED_NOTE_PRODUCTS
    assert PRODUCT_RESPONSE_SCHEMA_NAMES == {
        "timeline": "mena_summary_timeline",
        "action_items": "mena_summary_action_items",
        "general_summary": "mena_summary_general_summary",
    }


def test_note_package_assembles_and_validates_complete_three_product_result() -> None:
    request_records = _request_records()
    result = assemble_summary_result(
        product_payloads={
            "timeline": {
                "timeline": [
                    {
                        "text": "Discussed database migration before Friday.",
                        "source_segment_ids": ["merged_segment_000001"],
                    }
                ]
            },
            "action_items": {
                "action_items": [
                    {"text": "Finish database migration.", "owner": "You", "due": "Friday"}
                ]
            },
            "general_summary": {"general_summary": "The team discussed migration risk."},
        },
        request_records=request_records,
        merged_transcript=_summary_request().merged_transcript,
    )

    assert result.timeline[0].text == "Discussed database migration before Friday."
    assert result.timeline[0].source_segment_ids == ("merged_segment_000001",)
    assert result.timeline[0].start_seconds == 0.0
    assert result.action_items == (
        SummaryActionItem(text="Finish database migration.", owner="You", due="Friday"),
    )
    assert result.general_summary == "The team discussed migration risk."
    assert result.request_records == request_records


def test_note_package_rejects_incomplete_product_assembly() -> None:
    with pytest.raises(SummarizationContractError, match="exactly the required note products"):
        assemble_summary_result(
            product_payloads={
                "timeline": {
                    "timeline": [
                        {"text": "One event.", "source_segment_ids": ["merged_segment_000001"]}
                    ]
                }
            },
            request_records=_request_records(products=("timeline",)),
            merged_transcript=_summary_request().merged_transcript,
        )


def test_note_package_assembly_validates_product_payload_shape() -> None:
    with pytest.raises(SummarizationContractError, match="unsupported fields"):
        assemble_summary_result(
            product_payloads={
                "timeline": {
                    "timeline": [
                        {"text": "One event.", "source_segment_ids": ["merged_segment_000001"]}
                    ],
                    "extra": True,
                },
                "action_items": {"action_items": []},
                "general_summary": {"general_summary": "A short summary."},
            },
            request_records=_request_records(),
            merged_transcript=_summary_request().merged_transcript,
        )


def test_note_package_assembly_rejects_unknown_timeline_source_segment_id() -> None:
    with pytest.raises(SummarizationContractError, match="unknown ids: missing_segment"):
        assemble_summary_result(
            product_payloads={
                "timeline": {
                    "timeline": [{"text": "One event.", "source_segment_ids": ["missing_segment"]}]
                },
                "action_items": {"action_items": []},
                "general_summary": {"general_summary": "A short summary."},
            },
            request_records=_request_records(),
            merged_transcript=_summary_request().merged_transcript,
        )


def test_note_package_builds_provider_independent_one_shot_prompt() -> None:
    request = _summary_request()

    system = product_system_prompt("action_items", "de")
    user = json.loads(product_user_prompt(request))

    assert "Return only key action_items" in system
    assert "Write every user-facing string in de." in system
    assert user["task"] == "meeting_notes"
    assert user["language"] == request.policy.language
    assert user["transcript"]["segments"][0]["speaker"] == "You"
    assert user["manual_notes"] == request.manual_notes


def test_note_package_rejects_unsupported_product_payload() -> None:
    with pytest.raises(SummarizationContractError, match="Unsupported note product"):
        validate_product_payload("typo", {"general_summary": "Looks valid otherwise."})


def test_note_package_rejects_extra_product_payload_fields() -> None:
    with pytest.raises(SummarizationContractError, match="unsupported fields"):
        validate_product_payload(
            "timeline",
            {
                "timeline": [
                    {"text": "One event.", "source_segment_ids": ["merged_segment_000001"]}
                ],
                "extra": True,
            },
        )


def test_note_package_rejects_extra_action_item_fields() -> None:
    with pytest.raises(SummarizationContractError, match="unsupported fields"):
        validate_product_payload(
            "action_items",
            {
                "action_items": [
                    {"text": "Finish the migration.", "owner": None, "due": None, "extra": True}
                ]
            },
        )


def test_note_package_map_payload_validation_allows_empty_chunk_results() -> None:
    validate_product_map_payload("timeline", {"timeline": []})
    validate_product_map_payload("action_items", {"action_items": []})
    validate_product_map_payload("general_summary", {"general_summary": ""})


def test_note_package_map_payload_validation_rejects_invalid_empty_candidate_shape() -> None:
    with pytest.raises(SummarizationContractError, match="chunk candidate must be a string"):
        validate_product_map_payload("general_summary", {"general_summary": None})


def test_note_package_keeps_policy_result_and_language_validation_together() -> None:
    assert SummarizationPolicy(language="de_CH").language == "de-CH"
    result = SummaryResult.from_dict(_summary_result().to_dict())

    assert result.to_dict()["request_records"]
    with pytest.raises(SummarizationContractError, match="drifted to English"):
        assert_summary_output_language(
            SummaryResult(
                timeline=(
                    SummaryTimelineEntry(
                        text="The team discussed the database and the migration before Friday.",
                        source_segment_ids=("merged_segment_000001",),
                        start_seconds=0.0,
                    ),
                ),
                action_items=(),
                general_summary="Die Zusammenfassung ist deutsch.",
                request_records=_request_records(),
            ),
            "de",
        )


def _request_records(
    *, products: tuple[str, ...] = REQUIRED_NOTE_PRODUCTS
) -> tuple[ProductRequestRecord, ...]:
    return tuple(
        ProductRequestRecord(
            product=product,
            model="test-model",
            num_ctx=8192,
            temperature=0,
            seed=0,
            prompt_eval_count=10,
            eval_count=5,
        )
        for product in products
    )
