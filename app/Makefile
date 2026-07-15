.PHONY: check format format-check hooks import-boundaries lint pre-commit pre-push test typecheck

check: format-check lint import-boundaries typecheck test

format:
	uv run ruff format src tests scripts

format-check:
	uv run ruff format --check src tests scripts

hooks:
	cd .. && uv run --project app pre-commit install --config .pre-commit-config.yaml --hook-type pre-commit --hook-type pre-push

lint:
	uv run ruff check src tests scripts

import-boundaries:
	uv run python scripts/check_cli_import_boundary.py

pre-commit:
	cd .. && uv run --project app pre-commit run --config .pre-commit-config.yaml --all-files

pre-push:
	cd .. && uv run --project app pre-commit run --config .pre-commit-config.yaml --hook-stage pre-push --all-files

typecheck:
	uv run mypy

test:
	uv run pytest
