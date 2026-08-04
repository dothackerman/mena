from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "prototypes/issue210_meaning_level_replay.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("issue210_meaning_level_replay", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

evaluate_package = MODULE.evaluate_package


SEGMENTS = [
    {
        "id": "s1",
        "start_seconds": 0.0,
        "end_seconds": 40.0,
        "text": "We reviewed the product sketch.",
    },
    {
        "id": "s2",
        "start_seconds": 40.0,
        "end_seconds": 80.0,
        "text": "The tutor replaces the annual course.",
    },
    {
        "id": "s3",
        "start_seconds": 80.0,
        "end_seconds": 120.0,
        "text": "I will send the presentation link.",
    },
]

RUBRIC = {
    "meeting_end_seconds": 120.0,
    "minimum_timeline_coverage_ratio": 0.9,
    "maximum_verbatim_timeline_ratio": 0.25,
    "required_topics": [
        {"name": "product-model", "source_segment_ids": ["s2"]},
        {"name": "presentation", "source_segment_ids": ["s3"]},
    ],
    "explicit_actions": [
        {
            "name": "send-presentation-link",
            "source_segment_ids": ["s3"],
            "owner_supported": False,
            "due_supported": False,
        }
    ],
}


def test_owner_feedback_evaluator_rejects_fragmentary_early_only_package() -> None:
    package = {
        "timeline": [
            {
                "text": "We reviewed the product sketch.",
                "source_segment_ids": ["s1"],
                "start_seconds": 0.0,
            }
        ],
        "action_items": [
            {
                "text": "The tutor replaces the annual course.",
                "source_segment_ids": ["s2"],
                "owner": None,
                "due": None,
            }
        ],
        "general_summary_sections": [],
    }

    result = evaluate_package(package=package, segments=SEGMENTS, rubric=RUBRIC)

    assert result["passed"] is False
    assert result["checks"] == {
        "timeline_has_intervals": False,
        "timeline_reaches_final_phase": False,
        "timeline_is_meaning_level": False,
        "required_topics_are_covered": False,
        "actions_are_explicit_commitments": False,
        "owners_and_due_dates_are_supported": True,
    }
    assert result["missing_topics"] == ["product-model", "presentation"]
    assert result["non_commitment_action_indexes"] == [0]


def test_owner_feedback_evaluator_accepts_grounded_meaning_level_package() -> None:
    package = {
        "timeline": [
            {
                "text": "The product model was clarified around the tutor.",
                "source_segment_ids": ["s1", "s2"],
                "start_seconds": 0.0,
                "end_seconds": 80.0,
            },
            {
                "text": "The meeting closed with preparation for the presentation.",
                "source_segment_ids": ["s3"],
                "start_seconds": 80.0,
                "end_seconds": 120.0,
            },
        ],
        "action_items": [
            {
                "text": "Send the presentation link.",
                "source_segment_ids": ["s3"],
                "owner": None,
                "due": None,
            }
        ],
        "general_summary_sections": [
            {
                "text": "The tutor replaces the annual course and a presentation will follow.",
                "source_segment_ids": ["s2", "s3"],
            }
        ],
    }

    result = evaluate_package(package=package, segments=SEGMENTS, rubric=RUBRIC)

    assert result["passed"] is True
    assert all(result["checks"].values())
    assert result["missing_topics"] == []
    assert result["non_commitment_action_indexes"] == []
