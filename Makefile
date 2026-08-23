.PHONY: check ci fmt lint typecheck test cov cov-all golden

check: fmt lint typecheck test

ci: lint typecheck cov

fmt:
	uv run ruff format src tests packages

lint:
	uv run ruff check src tests packages

typecheck:
	uv run mypy src packages/pipeline-exec/src

test:
	uv run pytest -q

cov:
	uv run pytest -q --cov=lockstep --cov=pipeline_exec --cov-report=term-missing --cov-fail-under=90

# The true figure, including the extracted executors that need a running application to cover.
cov-all:
	uv run pytest -q --cov=lockstep --cov=pipeline_exec --cov-report=term-missing \
	  --cov-config=/dev/null --cov-fail-under=0

# Rewrite the golden output tree after an intentional change. Read the diff before committing.
golden:
	LOCKSTEP_REGEN=1 uv run pytest tests/test_golden.py -q
