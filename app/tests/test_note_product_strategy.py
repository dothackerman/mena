import pytest

from mena.note_product_strategy import (
    ONE_SHOT_STRATEGY,
    REDUCER_STRATEGY,
    ChunkSpan,
    NoteProductStrategyError,
    estimate_prompt_tokens,
    plan_chunk_spans,
    prompt_token_budget,
    select_note_product_strategy,
)


def test_estimate_prompt_tokens_counts_utf8_bytes() -> None:
    assert estimate_prompt_tokens("abcd") == 4
    assert estimate_prompt_tokens("über") == 5
    assert estimate_prompt_tokens("") == 0


def test_prompt_token_budget_applies_fraction() -> None:
    assert prompt_token_budget(num_ctx=8192, budget_fraction=0.85) == 6963
    assert prompt_token_budget(num_ctx=10, budget_fraction=1.0) == 10


@pytest.mark.parametrize(
    ("num_ctx", "budget_fraction"),
    [(0, 0.85), (8192, 0.0), (8192, 1.5), (-1, 0.85)],
)
def test_prompt_token_budget_rejects_invalid_inputs(num_ctx: int, budget_fraction: float) -> None:
    with pytest.raises(NoteProductStrategyError):
        prompt_token_budget(num_ctx=num_ctx, budget_fraction=budget_fraction)


def test_select_note_product_strategy_keeps_short_input_one_shot() -> None:
    assert select_note_product_strategy(prompt_estimate=100, prompt_budget=100) == ONE_SHOT_STRATEGY
    assert select_note_product_strategy(prompt_estimate=0, prompt_budget=1) == ONE_SHOT_STRATEGY


def test_select_note_product_strategy_routes_over_budget_input_to_reducer() -> None:
    assert select_note_product_strategy(prompt_estimate=101, prompt_budget=100) == REDUCER_STRATEGY


@pytest.mark.parametrize(
    ("prompt_estimate", "prompt_budget"),
    [(-1, 100), (100, 0)],
)
def test_select_note_product_strategy_rejects_invalid_inputs(
    prompt_estimate: int,
    prompt_budget: int,
) -> None:
    with pytest.raises(NoteProductStrategyError):
        select_note_product_strategy(prompt_estimate=prompt_estimate, prompt_budget=prompt_budget)


def test_plan_chunk_spans_packs_consecutive_items_deterministically() -> None:
    spans = plan_chunk_spans(item_estimates=(40, 40, 30, 60, 10, 10), chunk_budget=100)

    assert spans == (
        ChunkSpan(index=0, start=0, end=2),
        ChunkSpan(index=1, start=2, end=5),
        ChunkSpan(index=2, start=5, end=6),
    )


def test_plan_chunk_spans_keeps_single_fitting_chunk() -> None:
    assert plan_chunk_spans(item_estimates=(10, 20, 30), chunk_budget=100) == (
        ChunkSpan(index=0, start=0, end=3),
    )


def test_plan_chunk_spans_places_exact_fit_boundaries() -> None:
    spans = plan_chunk_spans(item_estimates=(50, 50, 1), chunk_budget=100)

    assert spans == (
        ChunkSpan(index=0, start=0, end=2),
        ChunkSpan(index=1, start=2, end=3),
    )


def test_plan_chunk_spans_fails_loudly_when_one_item_exceeds_budget() -> None:
    with pytest.raises(NoteProductStrategyError, match="exceeds the chunk budget"):
        plan_chunk_spans(item_estimates=(10, 101, 10), chunk_budget=100)


@pytest.mark.parametrize(
    ("item_estimates", "chunk_budget", "match"),
    [
        ((), 100, "At least one item"),
        ((10,), 0, "chunk_budget"),
        ((10, -1), 100, "non-negative"),
    ],
)
def test_plan_chunk_spans_rejects_invalid_inputs(
    item_estimates: tuple[int, ...],
    chunk_budget: int,
    match: str,
) -> None:
    with pytest.raises(NoteProductStrategyError, match=match):
        plan_chunk_spans(item_estimates=item_estimates, chunk_budget=chunk_budget)
