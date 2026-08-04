from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[1] / "prototypes/issue210_bounded_ordinary_export.py"
SPEC = importlib.util.spec_from_file_location("issue210_bounded_ordinary_export", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

BoundExceeded = MODULE.BoundExceeded
Limits = MODULE.Limits
build_call_plan = MODULE.build_call_plan
checkpoint_id = MODULE.checkpoint_id
normalize_actions = MODULE.normalize_actions
normalize_timeline = MODULE.normalize_timeline
render_markdown = MODULE.render_markdown


SEGMENTS = [
    {"id": "s1", "start_seconds": 2.0, "end_seconds": 4.0, "speaker": "A", "text": "One"},
    {"id": "s2", "start_seconds": 7.0, "end_seconds": 9.0, "speaker": "B", "text": "Two"},
]


def test_call_plan_declares_finite_progress_and_refuses_excess() -> None:
    plan = build_call_plan(chunk_count=2, section_count=2, limits=Limits(max_calls=9))
    assert plan["maximum_calls"] == 9
    assert plan["stages"] == {
        "extraction": 6,
        "semantic_grouping": 0,
        "summary_planning": 1,
        "section_generation": 2,
        "repairs": 0,
    }
    assert plan["progress_measures"]["extraction"] == "remaining product/chunk pairs"

    with pytest.raises(BoundExceeded, match="planned_call_budget_exceeded"):
        build_call_plan(chunk_count=3, section_count=2, limits=Limits(max_calls=9))


def test_timeline_rejects_unknown_sources_derives_time_orders_and_caps() -> None:
    candidates = [
        {"text": "Later", "source_segment_ids": ["s2"]},
        {"text": "Unsupported", "source_segment_ids": ["missing"]},
        {"text": "Earlier", "source_segment_ids": ["s1"]},
        {"text": "Earlier", "source_segment_ids": ["s1"]},
    ]
    result = normalize_timeline(candidates, SEGMENTS, capacity=1)
    assert result["items"] == [
        {"text": "Earlier", "source_segment_ids": ["s1"], "start_seconds": 2.0}
    ]
    assert result["invalid_count"] == 1
    assert result["duplicate_count"] == 1
    assert result["omitted_count"] == 1


def test_actions_preserve_absent_owner_and_due_and_source_overlap_deduplicates() -> None:
    candidates = [
        {"text": "Send draft", "owner": None, "due": None, "source_segment_ids": ["s1"]},
        {
            "text": "Send draft",
            "owner": "invented",
            "due": "tomorrow",
            "source_segment_ids": ["s1"],
        },
    ]
    result = normalize_actions(candidates, SEGMENTS, capacity=5)
    assert result["items"] == [
        {"text": "Send draft", "owner": None, "due": None, "source_segment_ids": ["s1"]}
    ]
    assert result["duplicate_count"] == 1


def test_checkpoint_identity_is_stable_and_sensitive_to_configuration(tmp_path: Path) -> None:
    first = checkpoint_id({"source": "abc", "config": {"cap": 2}})
    assert first == checkpoint_id({"config": {"cap": 2}, "source": "abc"})
    assert first != checkpoint_id({"source": "abc", "config": {"cap": 3}})

    package = {
        "language": "en",
        "timeline": [{"text": "Started", "source_segment_ids": ["s1"], "start_seconds": 2.0}],
        "action_items": [
            {"text": "Send draft", "owner": None, "due": None, "source_segment_ids": ["s1"]}
        ],
        "general_summary": "A short recall aid.",
    }
    markdown = render_markdown(package)
    assert (
        markdown == "## Timeline\n\n- [00:02] Started\n\n## Action items\n\n- Send draft\n\n"
        "## General summary\n\nA short recall aid.\n"
    )
    path = tmp_path / "package.json"
    path.write_text(json.dumps(package))
    assert "source_segment_ids" in path.read_text()
