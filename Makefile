.PHONY: setup build-sandbox build-sandbox-node build-sandbox-full test test-unit test-integration test-e2e lint format quality run clean update-prices

update-prices:             ## Refresh vendored model pricing from LiteLLM
	python scripts/update_prices.py

setup:                     ## Install dependencies
	pip install -e ".[dev,redis]"

build-sandbox:             ## Build the sandbox Docker image
	docker build -t agent-forge-sandbox:latest -f agent_forge/sandbox/Dockerfile .

build-sandbox-node:        ## Build the Node-focused sandbox image
	docker build -t agent-forge-sandbox:node -f agent_forge/sandbox/Dockerfile.node .

build-sandbox-full:        ## Build the full Python+Node sandbox image
	docker build -t agent-forge-sandbox:full -f agent_forge/sandbox/Dockerfile.full .

test:                      ## Run all tests (excludes integration + e2e)
	pytest --cov=agent_forge --cov-report=term-missing -m "not integration and not e2e"

test-unit:                 ## Run unit tests only
	pytest tests/unit -v

test-integration:          ## Run integration tests (requires Docker)
	pytest tests/integration -v

test-e2e:                  ## Run e2e tests (requires GEMINI_API_KEY + Docker)
	pytest tests/e2e -v -m e2e

lint:                      ## Run linters
	ruff check agent_forge tests
	mypy agent_forge

format:                    ## Auto-format code
	ruff format agent_forge tests

quality:                   ## Run code quality checks (dead code + maintainability)
	vulture agent_forge/ vulture_whitelist.py --min-confidence 80
	radon cc agent_forge/ -n C -s
	radon mi agent_forge/ -n B -s

run:                       ## Run a demo task
	python -m agent_forge.cli run --task "Add input validation" --repo ./tests/fixtures/sample_repo

clean:                     ## Clean up containers and build artifacts
	docker rm -f $$(docker ps -aq --filter "ancestor=agent-forge-sandbox") 2>/dev/null || true
	rm -rf .pytest_cache htmlcov .mypy_cache dist *.egg-info
