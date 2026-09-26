.DEFAULT_GOAL := help
.PHONY: help install lint fmt typecheck test cov live test-min check-dropin check docs docs-build

# Oldest versions the drop-in modules claim to support (see module docstrings / README).
MIN_PYTHON := 3.10
MIN_DEPS := --with "openai==1.106.0" --with "pydantic==2.8.0"

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-13s %s\n", $$1, $$2}'

install: ## Create .venv with dev deps and install the git pre-commit hook
	uv sync
	uv run pre-commit install

lint: ## All pre-commit hooks on all files: ruff check, ruff format, pyright, hygiene (same as CI)
	uv run pre-commit run --all-files --show-diff-on-failure

fmt: ## Auto-fix lint issues and format
	uv run ruff check --fix .
	uv run ruff format .

typecheck: ## pyright only
	uv run pyright

test: ## Offline test suite
	uv run pytest

cov: ## Offline test suite with coverage report
	uv run pytest --cov --cov-report=term-missing

live: ## One real call; needs AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT (+ API key or Entra ID)
	uv run pytest -m live -s

test-min: ## Offline suite on the minimum Python / openai / pydantic versions
	uv run --isolated --no-project --python $(MIN_PYTHON) $(MIN_DEPS) \
		--with pytest --with pytest-asyncio --with azure-identity \
		pytest -q -p no:cacheprovider tests

check-dropin: ## Import the module alone in an empty dir with only its required deps
	@tmp=$$(mktemp -d) && cp azure_openai_utils.py $$tmp/ && cd $$tmp && \
		uv run --isolated --no-project --python $(MIN_PYTHON) $(MIN_DEPS) \
		python -c "import azure_openai_utils as m; print('drop-in OK:', m.__name__, m.__version__)"

check: lint test test-min check-dropin ## Everything CI runs (on one Python version)

# mkdocs-material pins mkdocs<2, so its MkDocs 2.0 notice is only noise here.
docs: ## Preview the docs site at http://127.0.0.1:8000 with live reload
	NO_MKDOCS_2_WARNING=1 uv run --only-group docs mkdocs serve

docs-build: ## Build the docs site into site/; fails on broken links and warnings (same as CI)
	NO_MKDOCS_2_WARNING=1 uv run --only-group docs mkdocs build --strict
