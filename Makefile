# Canonical developer entry points. Run `make help` for the list.
# All Python tooling goes through `uv run` per the project convention.

.DEFAULT_GOAL := help
.PHONY: help install test lint format format-check validate check config-example apm test-transcribe

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

install: ## Sync all (incl. dev) dependencies
	uv sync --dev

test: ## Run the test suite
	uv run pytest -q

test-transcribe: ## Subproject tests (needs uv sync --project transcribe)
	uv run --project transcribe pytest transcribe/tests -q

lint: ## Lint with ruff
	uv run ruff check src tests transcribe

format: ## Auto-format with ruff
	uv run ruff format src tests transcribe

format-check: ## Verify formatting without modifying files
	uv run ruff format --check src tests transcribe

validate: ## Fast structural checks (docker compose, py_compile, shell syntax)
	docker compose config --services >/dev/null
	uv run python -m py_compile $$(find src/nagare_clip -name '*.py')
	for f in scripts/*.sh; do bash -n "$$f" || exit 1; done

check: lint format-check validate test ## Everything CI runs

config-example: ## Regenerate config.example.yml from the config models
	uv run python -m nagare_clip.config --write-example

apm: ## Smoke-install the skills package into a temp video dir and audit it (needs the apm CLI; not in CI)
	@tmp=$$(mktemp -d) && trap 'rm -rf "$${tmp:?}"' EXIT && echo "smoke install in $$tmp" && cd "$$tmp" && git init -q && \
		apm install "$(CURDIR)/skills" --target claude,codex && \
		diff -r "$(CURDIR)/skills/.apm/skills" .claude/skills && \
		diff -r "$(CURDIR)/skills/.apm/skills" .agents/skills && \
		apm audit --ci
