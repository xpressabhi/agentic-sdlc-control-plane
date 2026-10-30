.PHONY: install test lint cov serve demo demo-all verify clean reset

install:
	uv sync --all-extras

test:
	uv run pytest

lint:
	uv run ruff check src tests

cov:
	uv run pytest --cov=agentic_sdlc --cov-report=term-missing

serve:
	uv run asdlc serve

demo:
	uv run asdlc demo

demo-all:
	uv run asdlc demo --all

verify: lint test
	@echo "Verification complete."

clean:
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

reset: clean
	rm -rf var
