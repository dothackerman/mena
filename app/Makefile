.PHONY: check format format-check hooks lint pre-commit pre-push test typecheck

check: format-check lint typecheck test

format:
	uv run ruff format src tests

format-check:
	uv run ruff format --check src tests

hooks:
	uv run pre-commit install --config ../.pre-commit-config.yaml --hook-type pre-commit --hook-type pre-push

lint:
	uv run ruff check src tests

pre-commit:
	uv run pre-commit run --config ../.pre-commit-config.yaml --all-files

pre-push:
	uv run pre-commit run --config ../.pre-commit-config.yaml --hook-stage pre-push --all-files

typecheck:
	uv run mypy

test:
	uv run pytest
