"""The lifecycle for this repository.

This file is the configuration. It is executed, not parsed — there is no schema, no manifest and
nothing generated from it. It replaces `.lockstep/pipeline.yaml` plus the seven directories that
used to sit beside it.

Keep it pure: it is imported to be inspected (`in-lockstep ls`) as well as run, so it may
construct objects and bind them, but it must not perform IO at import.

One consequence of configuration being code is worth stating where somebody will read it: this
file can rebind any adapter, remove any middleware, and grant any tool. That is why it is the
first entry in the protected-path deny list, and why it is loaded from a trusted ref rather than
from whichever branch is under review.
"""

from in_lockstep import Lockstep
from in_lockstep.adapters import PytestTest, RuffValidate
from in_lockstep.adapters.pytest_adapter import Test
from in_lockstep.adapters.ruff_adapter import Validate
from in_lockstep.adapters.sandbox import Sandbox
from in_lockstep.ai.pricing import default_table
from in_lockstep.core.policy import Policy
from in_lockstep.core.spend import Budget
from in_lockstep.middleware import CostBudget, otel
from in_lockstep.privileged.egress import EgressPolicy, UnsandboxedEgress
from in_lockstep.strategies import default_registry

lockstep = Lockstep.detect()

# -- deterministic verbs ------------------------------------------------------------
#
# Both run out of process. pytest executes conftest.py from this repository and ruff loads its
# configuration, so an in-process run would hand repository-authored Python the credentials this
# process holds.
lockstep.bind(Test, PytestTest(args=["-q", "--no-header"], sandbox=Sandbox()))
lockstep.bind(Validate, RuffValidate(sandbox=Sandbox()))

# -- policy -------------------------------------------------------------------------
#
# Contributions append and only ever tighten. There is no removal API: what this preserves is that
# taking a standard away is a visible diff, not that it is impossible.
lockstep.contribute(
    Policy(
        name="framework-floor",
        source="in-lockstep",
        # Ceilings, not targets. A merge takes the lowest of several rather than the last read.
        max_turns=12,
        scan_input="warn",
        deny_tools=("shell", "write_file"),
    )
)

# -- egress -------------------------------------------------------------------------
#
# THIS IS AN OPT-OUT FROM A CONTROL, and it is here rather than in an options object because the
# design's answer to "you cannot stop a repository weakening its own controls" is that weakening
# one has to be a legible line in a diff. This is that line.
#
# What it switches off: a review reads a diff authored by whoever opened the change, so the
# context package carries UNTRUSTED_EXTERNAL content, and egress enforcement is mandatory for it.
# Development happens on laptops with open internet, where no enforcement exists and the probe
# would refuse an asserted one — correctly. The alternative is running every local review inside
# a constrained container, which is a real answer and not the one this repository has chosen.
#
# What it costs: an injection that succeeds has somewhere to send what it read. `injection.py`'s
# own docstring is blunt about this — a scanner "is not a substitute for tool deny-lists, egress
# rules. Those are what actually bound a successful injection." The compensating controls here are
# that the shipped ToolSet is empty, the policy floor denies `shell` and `write_file`, and
# `ChangeGuard` stands at all three write paths. None of them is an egress rule.
#
# CI does not take this path: `.github/workflows/lockstep.yml` sets IN_LOCKSTEP_EGRESS=enforced,
# and the probe there has to pass.
lockstep.bind(EgressPolicy, UnsandboxedEgress())

# -- budgets ------------------------------------------------------------------------
#
# Per-run only. The per-agent-per-day ceiling the old substrate enforced before a run started has
# no in-process equivalent — see docs/controls-crosswalk.md, entry 3. The replacement is a
# provider-side organisation limit, which `doctor` can ask about but cannot verify.
#
# $0.25 rather than $2.00, sized against a measurement instead of a guess. A security review of a
# median diff costs about $0.02 and the pre-flight estimate for one is about $0.07 — the estimate
# bounds output by `max_tokens` rather than by an expected value, so it is deliberately the larger
# number. That leaves roughly a threefold margin over the estimate and tenfold over the actual
# spend, which is what a ceiling wants: far enough above normal that it never fires on a good run,
# close enough that a loop going wrong is stopped in cents rather than dollars.
#
# $2.00 was a hundredfold headroom, which is not a ceiling so much as a formality.
lockstep.budget = Budget(usd=0.25, wall_seconds=900)

# -- models and strategies ----------------------------------------------------------
lockstep.models.route("review", "anthropic:claude-sonnet-4-6")
lockstep.models.route("implement", "anthropic:claude-sonnet-4-6")
lockstep.models.route("triage", "local:qwen3-8b")

strategies = default_registry()

# -- cost ---------------------------------------------------------------------------
#
# An unpriced model is refused before the call rather than priced at some default. A defaulted
# rate is wrong in both directions and produces a number that looks like evidence.
costs = default_table()

# -- middleware ---------------------------------------------------------------------
#
# Redaction, egress policy and the kill switch are NOT here. They are privileged: they run outside
# this chain, so `--no-middleware` cannot reach them.
lockstep.middleware += [
    otel(),
    CostBudget(usd=2.00),
]
