.PHONY: check ci ci-compiler ci-framework fmt lint typecheck test cov cov-all cov-new golden fetch corpus

check: fmt lint typecheck test

# Two gates, deliberately separate. `ci-compiler` is wired to the thing being deleted — it depends
# on `fetch`, and it dies with `src/lockstep/` in phase 7. `ci-framework` has no such dependency,
# so the new package is never gated on the old one being installable.
ci: ci-framework ci-compiler

ci-compiler: lint typecheck cov
ci-framework: lint typecheck cov-new

fmt:
	uv run ruff format src tests packages conftest.py

lint:
	uv run ruff check src tests packages conftest.py

typecheck:
	uv run mypy src packages/pipeline-exec/src

# Runs the WORKING TREE compiler, deliberately, and an earlier attempt to pin it to the released
# 0.1.0 was wrong. A `lockstep:` upstream ships *inside* the compiler, so pinning the compiler
# also pins the inherited pipeline content — and this tree's library has moved past the release,
# so the committed output stopped matching what the spec compiles to. That is precisely the
# property `capabilities.compiler: "."` exists to protect, documented in .lockstep/pipeline.yaml:
# a gate that installed the published compiler would check a pull request against the previous
# one's library and pass.
#
# Decoupling the new package from the old one belongs at the CI target, not here: `ci-framework`
# never invokes this, so `in_lockstep` is still never gated on the compiler being installable.
#
# `.lockstep/` inherits lockstep:review, and inherited definitions resolve into `.pipeline/`, which
# is gitignored — resolved state, like a virtualenv. Every target that loads the spec depends on
# this rather than on somebody having remembered.
fetch:
	uv run lockstep fetch

test: fetch
	uv run pytest -q

# The compiler's gate. Unchanged, and frozen: it measures code that is on its way out.
cov: fetch
	uv run pytest -q --cov=lockstep --cov=pipeline_exec --cov-report=term-missing --cov-fail-under=90

# The framework's gate (GATE-TEST-3). Separate from `cov` so that deleting `lockstep` cannot mask
# `in_lockstep`'s number, and so the floor cannot stay green by measuring frozen code.
#
# Two-sided: below the floor fails, and rising more than 2 points above it fails AS A REQUIRED
# FLOOR UPDATE. A one-directional ratchet with a stale floor is a dead gate; a bare failure on the
# way up just teaches everyone to bump the number until CI is green, so the message says what to do.
# `--cov-config=/dev/null` is deliberate: cov-new must not inherit pyproject's `omit` list, which is
# how the existing 90% gate stays comfortable.
cov-new:
	@floor=$$(cat .coverage-floor); \
	uv run pytest -q tests/in_lockstep tests/characterization \
	  --cov=in_lockstep --cov-config=/dev/null --cov-report=term-missing \
	  --cov-fail-under=$$floor || exit 1; \
	actual=$$(uv run python -c "import json,subprocess; \
	print(int(json.loads(subprocess.run(['uv','run','coverage','json','-o','-','--quiet'], \
	capture_output=True,text=True).stdout)['totals']['percent_covered']))" 2>/dev/null || echo $$floor); \
	if [ "$$actual" -gt "$$((floor + 2))" ]; then \
	  echo ""; \
	  echo "coverage is $$actual%, floor is $$floor%."; \
	  echo "Bump it:  echo $$actual > .coverage-floor"; \
	  exit 1; \
	fi; \
	echo "in_lockstep coverage $$actual% (floor $$floor%)"

# The true figure, including the extracted executors that need a running application to cover.
cov-all: fetch
	uv run pytest -q --cov=lockstep --cov=pipeline_exec --cov-report=term-missing \
	  --cov-config=/dev/null --cov-fail-under=0

# Rewrite the golden output tree after an intentional change. Read the diff before committing.
# FROZEN from phase 0 (GATE-TEST-4): a golden test you may regenerate during a pivot is not a test,
# so this must not run in CI.
golden: fetch
	LOCKSTEP_REGEN=1 uv run pytest tests/test_golden.py -q

# Re-capture the composed-prompt characterization corpus. Only meaningful while the compiler runs.
corpus: fetch
	uv run python tools/capture_corpus.py
