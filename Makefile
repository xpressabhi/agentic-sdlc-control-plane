.PHONY: install test lint cov serve demo demo-all verify clean reset

install:
	uv sync --all-extras

test: install
	uv run pytest

lint: install
	uv run ruff check src tests

cov: install
	uv run pytest --cov=agentic_sdlc --cov-report=term-missing

serve: install
	uv run asdlc serve

demo: install
	uv run asdlc demo

demo-all: install
	uv run asdlc demo --all

verify: install lint test
	@echo "Verification complete."

clean:
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

reset: clean
	rm -rf var
