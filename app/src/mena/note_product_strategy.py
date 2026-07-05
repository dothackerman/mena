"""Provider-owned note-product execution strategy selection and chunk planning.

This module owns the budget arithmetic behind the note-product execution
strategy (see CONTEXT.md): whether one note product runs as a single
``one_shot`` generation or as a chunked map/reduce ``reducer`` generation.
The capability resolver still owns *which* provider runs the summarization
stage (ADR-010, #120); this module never selects, validates, or falls back
between providers. Providers call into it to decide *how* to run one product
given their own context-window constraints (ADR-035).

All arithmetic is deterministic and token-estimate based so routing and the
pre-send hard-fail budget guard share one source of truth (#108, #113).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

ONE_SHOT_STRATEGY = "one_shot"
REDUCER_STRATEGY = "reducer"
NOTE_PRODUCT_STRATEGIES = frozenset({ONE_SHOT_STRATEGY, REDUCER_STRATEGY})

ONE_SHOT_CALL = "one_shot"
MAP_CALL = "map"
REDUCE_CALL = "reduce"
NOTE_PRODUCT_CALLS = frozenset({ONE_SHOT_CALL, MAP_CALL, REDUCE_CALL})


class NoteProductStrategyError(RuntimeError):
    """Raised when strategy or chunk planning inputs cannot be satisfied."""


@dataclass(frozen=True)
class ChunkSpan:
    """One planned transcript chunk as a half-open item-index range."""

    index: int
    start: int
    end: int


def estimate_prompt_tokens(text: str) -> int:
    """Return a conservative token upper bound for one prompt text.

    Ollama exposes no preflight tokenizer, so we count UTF-8 bytes: a hard
    upper bound for byte-fallback tokenizers that avoids ``len(text) / 4``
    undercounts on short words, IDs, and punctuation-heavy notes. The
    overshoot is intentional — it routes long inputs to the reducer earlier
    rather than risking silent head truncation.
    """

    return len(text.encode("utf-8"))


def prompt_token_budget(*, num_ctx: int, budget_fraction: float) -> int:
    """Return the prompt-token budget carved out of one context window."""

    if num_ctx < 1:
        raise NoteProductStrategyError("num_ctx must be at least 1.")
    if not 0 < budget_fraction <= 1:
        raise NoteProductStrategyError("budget_fraction must be in (0, 1].")
    return int(num_ctx * budget_fraction)


def select_note_product_strategy(*, prompt_estimate: int, prompt_budget: int) -> str:
    """Return the execution strategy for one note product from budget arithmetic.

    A one-shot prompt that fits the budget stays one-shot; anything larger
    routes to the reducer. Callers must pass the same estimate the pre-send
    budget guard would use so routing can never disagree with the guard.
    """

    if prompt_estimate < 0:
        raise NoteProductStrategyError("prompt_estimate must be non-negative.")
    if prompt_budget < 1:
        raise NoteProductStrategyError("prompt_budget must be at least 1.")
    if prompt_estimate <= prompt_budget:
        return ONE_SHOT_STRATEGY
    return REDUCER_STRATEGY


def plan_chunk_spans(
    *,
    item_estimates: Sequence[int],
    chunk_budget: int,
) -> tuple[ChunkSpan, ...]:
    """Greedily pack consecutive items into chunks that fit one chunk budget.

    Items are transcript segments in transcript order; estimates are the
    per-item prompt-token estimates including any serialization overhead the
    caller accounts for. Fails loudly when a single item alone exceeds the
    budget — splitting inside a segment would break segment grounding.
    """

    if chunk_budget < 1:
        raise NoteProductStrategyError("chunk_budget must be at least 1.")
    if not item_estimates:
        raise NoteProductStrategyError("At least one item is required to plan chunks.")

    spans: list[ChunkSpan] = []
    start = 0
    used = 0
    for position, estimate in enumerate(item_estimates):
        if estimate < 0:
            raise NoteProductStrategyError("Item estimates must be non-negative.")
        if estimate > chunk_budget:
            raise NoteProductStrategyError(
                f"Item {position} prompt estimate ({estimate}) exceeds the chunk "
                f"budget ({chunk_budget}); one transcript segment alone does not "
                "fit the configured context budget."
            )
        if position > start and used + estimate > chunk_budget:
            spans.append(ChunkSpan(index=len(spans), start=start, end=position))
            start = position
            used = 0
        used += estimate
    spans.append(ChunkSpan(index=len(spans), start=start, end=len(item_estimates)))
    return tuple(spans)
