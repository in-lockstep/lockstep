.PHONY: check ci fmt lint typecheck test cov

check: fmt lint typecheck test

ci: lint typecheck cov

fmt:
	uv run ruff format src tests

lint:
	uv run ruff check src tests

typecheck:
	uv run mypy src

test:
	uv run pytest -q

# One gate now. The compiler's separate gate went with the compiler, and so did the reason the two
# were split: `ci-framework` existed so the new package was never blocked on the old one being
# installable, and there is no longer an old one.
#
# Two-sided. Below the floor fails; more than two points above it fails AS A REQUIRED FLOOR
# UPDATE, with the command to run, because a bare failure on the way up teaches everyone to bump
# the number until CI is green. A one-directional ratchet with a stale floor is a dead gate.
#
# --cov-config=/dev/null is deliberate: this must not inherit an `omit` list, which is how a
# coverage gate stays comfortable while measuring less and less. The number is therefore lower
# than the old gate's 90 and means more.
#
# The floor moved DOWN once, 69 -> 68, when `pipeline_exec` was deleted. That is the one direction
# a ratchet is built to refuse, so it is recorded rather than left to look like erosion: nothing
# about `in_lockstep`'s coverage changed. The old number was a blend of two packages and the better
# covered one is gone, so 68 is what this repository's own code has always measured.
#
# The rcfile is passed to BOTH halves. Passing it to only one made the floor check and the ratchet
# read different numbers off the same run, which is a gate that contradicts itself.
cov:
	@floor=$$(cat .coverage-floor); \
	uv run pytest -q --cov=in_lockstep --cov-config=/dev/null \
	  --cov-report=term-missing --cov-fail-under=$$floor || exit 1; \
	actual=$$(uv run python -c "import json,subprocess; \
	print(int(json.loads(subprocess.run(['uv','run','coverage','json','--rcfile=/dev/null', \
	'-o','-','--quiet'],capture_output=True,text=True).stdout)['totals']['percent_covered']))" \
	2>/dev/null || echo $$floor); \
	if [ "$$actual" -gt "$$((floor + 2))" ]; then \
	  echo ""; \
	  echo "coverage is $$actual%, floor is $$floor%."; \
	  echo "Bump it:  echo $$actual > .coverage-floor"; \
	  exit 1; \
	fi; \
	echo "coverage $$actual% (floor $$floor%)"
