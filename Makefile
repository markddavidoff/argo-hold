.PHONY: help test test-k3d lint

help: ## List targets
	@grep -E '^[a-z0-9-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

test: ## Run the pytest suite (fake kubectl; no cluster needed)
	python3 -m pytest tests

test-k3d: ## Opt-in: integration tests on a throwaway local k3d + Argo CD (docker, k3d, network)
	@command -v k3d >/dev/null 2>&1 || { echo "k3d not on PATH"; exit 1; }
	@bash tests/k3d/setup.sh && ARGO_HOLD_K3D=1 python3 -m pytest tests -m k3d; rc=$$?; bash tests/k3d/teardown.sh; exit $$rc

lint: ## Lint with ruff (needs ruff, or uv: `uvx ruff check .`)
	ruff check .
