.PHONY: check format lint test typecheck

check: lint typecheck test

format:
	uv run ruff format src tests

lint:
	uv run ruff check src tests

typecheck:
	uv run mypy

test:
	uv run pytest

