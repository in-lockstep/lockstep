.PHONY: check ci fmt lint typecheck test cov cov-all golden fetch

check: fmt lint typecheck test

ci: lint typecheck cov

fmt:
	uv run ruff format src tests packages conftest.py

lint:
	uv run ruff check src tests packages conftest.py

typecheck:
	uv run mypy src packages/pipeline-exec/src

# `.lockstep/` inherits lockstep:review, and inherited definitions resolve into `.pipeline/`, which
# is gitignored — resolved state, like a virtualenv. So the self-host tests cannot load this
# repository's own spec from a fresh clone until this has run, and every target that loads it
# depends on this rather than on somebody having remembered.
#
# Offline and instant here: a `lockstep:` upstream ships inside the compiler, so there is nothing to
# download and no network to be unavailable.
fetch:
	uv run lockstep fetch

test: fetch
	uv run pytest -q

cov: fetch
	uv run pytest -q --cov=lockstep --cov=pipeline_exec --cov-report=term-missing --cov-fail-under=90

# The true figure, including the extracted executors that need a running application to cover.
cov-all: fetch
	uv run pytest -q --cov=lockstep --cov=pipeline_exec --cov-report=term-missing \
	  --cov-config=/dev/null --cov-fail-under=0

# Rewrite the golden output tree after an intentional change. Read the diff before committing.
golden: fetch
	LOCKSTEP_REGEN=1 uv run pytest tests/test_golden.py -q
