.PHONY: setup build-sandbox test test-unit test-integration lint format run clean

setup:                     ## Install dependencies
	pip install -e ".[dev,redis]"

build-sandbox:             ## Build the sandbox Docker image
	docker build -t agent-forge-sandbox:latest -f agent_forge/sandbox/Dockerfile .

test:                      ## Run all tests
	pytest --cov=agent_forge --cov-report=term-missing

test-unit:                 ## Run unit tests only
	pytest tests/unit -v

test-integration:          ## Run integration tests (requires Docker)
	pytest tests/integration -v

lint:                      ## Run linters
	ruff check agent_forge tests
	mypy agent_forge

format:                    ## Auto-format code
	ruff format agent_forge tests

run:                       ## Run a demo task
	python -m agent_forge.cli run --task "Add input validation" --repo ./tests/fixtures/sample_repo

clean:                     ## Clean up containers and build artifacts
	docker rm -f $$(docker ps -aq --filter "ancestor=agent-forge-sandbox") 2>/dev/null || true
	rm -rf .pytest_cache htmlcov .mypy_cache dist *.egg-info
