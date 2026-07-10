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
    assemble_summary_result,
    assert_summary_output_language,
    product_system_prompt,
    product_user_prompt,
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
            "timeline": {"timeline": ["Discussed database migration before Friday."]},
            "action_items": {
                "action_items": [
                    {"text": "Finish database migration.", "owner": "You", "due": "Friday"}
                ]
            },
            "general_summary": {"general_summary": "The team discussed migration risk."},
        },
        request_records=request_records,
    )

    assert result.timeline == ("Discussed database migration before Friday.",)
    assert result.action_items == (
        SummaryActionItem(text="Finish database migration.", owner="You", due="Friday"),
    )
    assert result.general_summary == "The team discussed migration risk."
    assert result.request_records == request_records


def test_note_package_rejects_incomplete_product_assembly() -> None:
    with pytest.raises(SummarizationContractError, match="exactly the required note products"):
        assemble_summary_result(
            product_payloads={"timeline": {"timeline": ["One event."]}},
            request_records=_request_records(products=("timeline",)),
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


def test_note_package_keeps_policy_result_and_language_validation_together() -> None:
    assert SummarizationPolicy(language="de_CH").language == "de-CH"
    result = SummaryResult.from_dict(_summary_result().to_dict())

    assert result.to_dict()["request_records"]
    with pytest.raises(SummarizationContractError, match="drifted to English"):
        assert_summary_output_language(
            SummaryResult(
                timeline=("The team discussed the database and the migration before Friday.",),
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
