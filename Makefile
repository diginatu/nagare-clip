# Canonical developer entry points. Run `make help` for the list.
# All Python tooling goes through `uv run` per the project convention.

.DEFAULT_GOAL := help
.PHONY: help install test lint format format-check validate check config-example

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

install: ## Sync all (incl. dev) dependencies
	uv sync --dev

test: ## Run the test suite
	uv run pytest -q

lint: ## Lint with ruff
	uv run ruff check src tests

format: ## Auto-format with ruff
	uv run ruff format src tests

format-check: ## Verify formatting without modifying files
	uv run ruff format --check src tests

validate: ## Fast structural checks (docker compose, py_compile, shell syntax)
	docker compose config --services >/dev/null
	uv run python -m py_compile $$(find src/nagare_clip -name '*.py')
	bash -n scripts/run_pipeline.sh

check: lint format-check validate test ## Everything CI runs

config-example: ## Regenerate config.example.yml from the config models
	uv run python -m nagare_clip.config --write-example
