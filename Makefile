BUNDLE_NAME = synapse-research
VERSION ?= 0.1.0

.PHONY: help install format lint typecheck test check run run-http bundle clean bump dev build-ui

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install the package with dev dependencies
	uv sync

dev: ## Start UI dev server (Vite + MCP server)
	cd ui && npm run dev

build-ui: ## Build UI for production
	cd ui && npm install && npm run build

format: ## Format code with ruff
	uv run ruff format src/ tests/

lint: ## Lint code with ruff
	uv run ruff check src/ tests/

typecheck: ## Type check with ty
	uv run ty check src/

test: ## Run tests
	uv run pytest tests/ -v

check: lint test ## Run all checks

run: ## Run in stdio mode
	uv run python -m mcp_research.server

run-http: ## Run HTTP server with uvicorn
	uv run uvicorn mcp_research.server:app --host 0.0.0.0 --port 8002 --reload

bundle: clean-bundle ## Build MCPB bundle
	@rm -rf deps/
	@uv pip install --target ./deps --only-binary :all: . 2>/dev/null || uv pip install --target ./deps .
	mcpb validate manifest.json
	mcpb pack . nimblebraininc-research-$(VERSION)-$$(uname -s | tr '[:upper:]' '[:lower:]')-$$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/').mcpb

clean-bundle: ## Remove build artifacts
	rm -rf deps/ *.mcpb

clean: clean-bundle ## Clean everything
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true

bump: ## Bump version (usage: make bump VERSION=0.2.0)
	@if [ -z "$(VERSION)" ]; then echo "Usage: make bump VERSION=x.y.z"; exit 1; fi
	@jq --arg v "$(VERSION)" '.version = $$v' manifest.json > manifest.tmp.json && mv manifest.tmp.json manifest.json
	@sed -i '' 's/^version = ".*"/version = "$(VERSION)"/' pyproject.toml
	@sed -i '' 's/^__version__ = ".*"/__version__ = "$(VERSION)"/' src/mcp_research/__init__.py
	@echo "Bumped to $(VERSION)"
