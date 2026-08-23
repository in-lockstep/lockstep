.PHONY: check ci fmt lint typecheck test cov golden

check: fmt lint typecheck test

ci: lint typecheck cov

fmt:
	uv run ruff format src tests

lint:
	uv run ruff check src tests

typecheck:
	uv run mypy src

test:
	uv run pytest tests -q

cov:
	uv run pytest tests -q --cov=lockstep --cov-report=term-missing --cov-fail-under=90

# Rewrite the golden output tree after an intentional change. Read the diff before committing.
golden:
	LOCKSTEP_REGEN=1 uv run pytest tests/test_golden.py -q
