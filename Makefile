PY ?= python

.PHONY: help install lint typecheck test check run mock docker mcp probe seed clean

help:            ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

install:         ## editable install with dev tools
	$(PY) -m pip install -e ".[dev]"

lint:            ## ruff lint + format check
	ruff check .
	ruff format --check .

typecheck:       ## mypy
	mypy app

test:            ## pytest (fully offline: mock Airwallex + scripted analyst)
	pytest -q

check: lint typecheck test  ## everything CI runs except docker

run:             ## dev server with whatever env vars are set
	uvicorn app.main:app --reload --port 8000

mock:            ## dev server on the mock sandbox and the deterministic analyst
	AIRWALLEX_MODE=mock LLM_MOCK=1 uvicorn app.main:app --reload --port 8000

mcp:             ## MCP server on stdio (for Claude Code / Cursor)
	$(PY) -m app.mcp_server

probe:           ## read-only shape probe of a live sandbox (needs keys)
	$(PY) scripts/probe_airwallex.py

seed:            ## stage demo incidents in YOUR live sandbox (needs keys)
	$(PY) scripts/seed_sandbox.py

docker:          ## build and run the container in mock mode
	docker build -t ledgerguard .
	docker run --rm -p 8000:8000 -e AIRWALLEX_MODE=mock -e LLM_MOCK=1 ledgerguard

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
