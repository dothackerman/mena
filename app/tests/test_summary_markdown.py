from mena.processing import ArtifactRef
from mena.summarization import (
    FakeSummarizationProvider,
    SummarizationPolicy,
    SummaryActionItem,
    SummaryArtifact,
    SummaryResult,
)
from mena.summary_markdown import format_summary_markdown

SESSION_ID = "00000000-0000-0000-0000-000000000001"


def test_summary_markdown_renders_sections_in_order_with_exact_headings() -> None:
    markdown = format_summary_markdown(_summary_artifact())

    assert [line for line in markdown.splitlines() if line.startswith("## ")] == [
        "## Timeline",
        "## Important points",
        "## Action items",
        "## General summary",
    ]
    assert markdown == (
        "## Timeline\n"
        "\n"
        "- Discussed database migration before Friday.\n"
        "- Confirmed release risk.\n"
        "\n"
        "## Important points\n"
        "\n"
        "- Release is blocked until migration is complete.\n"
        "\n"
        "## Action items\n"
        "\n"
        "- Finish database migration. (Owner: You; Due: Friday)\n"
        "- Schedule rollback review.\n"
        "\n"
        "## General summary\n"
        "\n"
        "The meeting focused on database migration risk.\n"
    )


def test_summary_markdown_renders_action_items_with_optional_owner_and_due() -> None:
    markdown = format_summary_markdown(
        _summary_artifact(
            action_items=(
                SummaryActionItem(text="Finish database migration.", owner="You", due="Friday"),
                SummaryActionItem(text="Schedule rollback review.", owner="PM", due=None),
                SummaryActionItem(text="Share launch checklist.", owner=None, due="Thursday"),
                SummaryActionItem(text="Capture open questions.", owner=None, due=None),
            )
        )
    )

    assert "Finish database migration. (Owner: You; Due: Friday)" in markdown
    assert "Schedule rollback review. (Owner: PM)" in markdown
    assert "Share launch checklist. (Due: Thursday)" in markdown
    assert "- Capture open questions.\n" in markdown


def test_summary_markdown_uses_german_chrome_for_german_summary_artifact() -> None:
    markdown = format_summary_markdown(
        _summary_artifact(
            language="de",
            action_items=(
                SummaryActionItem(
                    text="Datenbankmigration abschliessen.",
                    owner="Du",
                    due="Freitag",
                ),
            ),
        )
    )

    assert [line for line in markdown.splitlines() if line.startswith("## ")] == [
        "## Zeitverlauf",
        "## Wichtige Punkte",
        "## Aufgaben",
        "## Zusammenfassung",
    ]
    assert "- Datenbankmigration abschliessen. (Verantwortlich: Du; Faellig: Freitag)" in markdown
    assert "## Timeline" not in markdown
    assert "Owner:" not in markdown


def test_summary_markdown_accepts_summary_result_payload() -> None:
    markdown = format_summary_markdown(_summary_result())

    assert markdown.startswith("## Timeline\n\n- Discussed database migration before Friday.")


def test_summary_markdown_accepts_summary_artifact_mapping() -> None:
    markdown = format_summary_markdown(_summary_artifact().to_dict())

    assert markdown.startswith("## Timeline\n\n- Discussed database migration before Friday.")


def test_summary_markdown_accepts_summary_result_mapping() -> None:
    markdown = format_summary_markdown(_summary_result().to_dict())

    assert markdown.startswith("## Timeline\n\n- Discussed database migration before Friday.")


def _summary_artifact(
    *,
    action_items: tuple[SummaryActionItem, ...] | None = None,
    language: str = "en",
) -> SummaryArtifact:
    result = _summary_result(action_items=action_items)
    return SummaryArtifact(
        version=1,
        session_id=SESSION_ID,
        merged_transcript_artifact=ArtifactRef(
            id=f"{SESSION_ID}:artifacts/merged_transcript/merged.json",
            artifact_type="merged_transcript",
            version=1,
            sha256="1" * 64,
        ),
        manual_notes_artifact=None,
        policy=SummarizationPolicy(language=language).to_dict(),
        provider=FakeSummarizationProvider(result=result).metadata,
        timeline=result.timeline,
        important_points=result.important_points,
        action_items=result.action_items,
        general_summary=result.general_summary,
        created_at="2026-05-18T17:00:00Z",
    )


def _summary_result(
    *,
    action_items: tuple[SummaryActionItem, ...] | None = None,
) -> SummaryResult:
    return SummaryResult(
        timeline=(
            "Discussed database migration before Friday.",
            "Confirmed release risk.",
        ),
        important_points=("Release is blocked until migration is complete.",),
        action_items=(
            action_items
            if action_items is not None
            else (
                SummaryActionItem(text="Finish database migration.", owner="You", due="Friday"),
                SummaryActionItem(text="Schedule rollback review.", owner=None, due=None),
            )
        ),
        general_summary="The meeting focused on database migration risk.",
    )
