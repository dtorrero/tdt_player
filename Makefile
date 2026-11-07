# Standard project verbs. Install tools with: make tools
PYTHON ?= python3
KANBAN_PORT ?= 3484

.DEFAULT_GOAL := help

.PHONY: help tools install lint format test run cli clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

tools: ## Print how to install ruff / pre-commit / pytest
	@echo "Ruff:        pipx install ruff   (or: sudo pacman -S ruff)"
	@echo "Pre-commit:  pipx install pre-commit && pre-commit install"
	@echo "Pytest:      python3 -m pip install --user pytest"

install: ## Install dev tooling via pipx where available
	@command -v pipx >/dev/null 2>&1 && { pipx install ruff || true; pipx install pre-commit || true; } || echo "pipx not found; see 'make tools'"

lint: ## Lint Python (ruff) + shell syntax check
	@if command -v ruff >/dev/null 2>&1; then ruff check . && ruff format --check . ; else echo "ruff not found -> 'make tools'"; fi
	@for s in scripts/*.sh packaging/*.sh; do [ -f "$$s" ] && bash -n "$$s" || true; done

format: ## Auto-fix + format Python with ruff
	@if command -v ruff >/dev/null 2>&1; then ruff check --fix . && ruff format . ; else echo "ruff not found -> 'make tools'"; fi

test: ## Run the test suite
	@if $(PYTHON) -c "import pytest" 2>/dev/null; then $(PYTHON) -m pytest -q ; else echo "pytest not found -> 'make tools'"; fi

run: ## Run the GUI (python -m tdt_player)
	$(PYTHON) -m tdt_player

cli: ## Run the CLI (python -m tdt_player.cli --help)
	$(PYTHON) -m tdt_player.cli --help

clean: ## Remove caches and build artifacts
	@rm -rf .pytest_cache .ruff_cache .mypy_cache build dist ./*.egg-info
	@find . -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "cleaned"
