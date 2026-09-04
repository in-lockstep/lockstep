.PHONY: check ci fmt lint typecheck test cov evidence

check: fmt lint typecheck test evidence

ci: lint typecheck cov evidence

fmt:
	uv run ruff format src tests

lint:
	uv run ruff check src tests

typecheck:
	uv run mypy src

test:
	uv run pytest -q

# The promoted corpus, settled on every commit. Offline, no key, nothing billed: a harvested case
# carries the answer its expectations came from, so this replays nothing over a network.
#
# It runs while `evidence/cases/` is empty, and that is deliberate rather than an oversight. The
# wiring is what makes the first promotion measured on the commit that makes it, instead of being
# a file somebody has to remember to point a command at. `eval run` over nothing says `pass rate
# n/a -- nothing decided`, which is the honest reading and not a green tick.
#
# Not folded into `test`: pytest asserts things about the framework, and this settles the
# framework's own recorded work against what a model actually said. Different subject, and worth
# seeing separately in the output.
evidence:
	uv run in-lockstep eval run --corpus evidence/cases

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
# GATE-TEST-3 lives here rather than in pytest: it is a property of the run, not of a test.
#
# `--cov=src/in_lockstep`, a PATH, and not `--cov=in_lockstep`, a module name. The module name
# measures anything imported into that namespace, and `init` scaffolds a `.lockstep/lockstep.py`
# that loads as `in_lockstep._lifecycle` — so eighty test-written scaffold files under pytest's
# tmp_path were in the denominator, contributing 838 statements nothing was ever going to cover.
# The gate therefore moved whenever the scaffold changed size, which is a gate measuring the
# fixtures rather than the code. It also swept in this repository's own `.lockstep/lockstep.py`,
# which is configuration.
#
# The floor moved UP, 88 -> 89, in the same change and for that reason. Read the direction
# carefully: this is stricter, not more comfortable. 88% was a blend of the package with a thousand
# statements of generated fixtures; 89.68% is what `src/in_lockstep` actually measures, and there is
# nowhere left to hide a drop behind a bigger scaffold.
cov:
	@floor=$$(cat .coverage-floor); \
	uv run pytest -q --cov=src/in_lockstep --cov-config=/dev/null \
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
