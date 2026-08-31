# Cookbook

Ten recipes, twenty lines or fewer each. Every `lockstep.py` snippet is executed by the test
suite, so a recipe that stops matching the API fails CI rather than a reader. Snippets go in
`.lockstep/lockstep.py` unless a recipe says otherwise.

## 1. Route triage to a local model — the $0 path

The `local` provider ships pointed at Ollama and registered `free`, so nothing needs a price and
the run bills exactly zero — while tokens are still counted, because free is not unmeasured.

```python
from in_lockstep import Lockstep
from in_lockstep.core.verbs import Verb

lockstep = Lockstep.detect()
lockstep.models.route(Verb.TRIAGE, "local:qwen3-8b")
```

```bash
in-lockstep triage --ticket '#42'
```

## 2. Go keyless in CI — workload identity federation

Delete `ANTHROPIC_API_KEY` from repository secrets. Give the model-calling job an identity
instead, and hand the framework the identifiers of the federation rule that accepts it — plain
env, **not** secrets: an identifier in `secrets` seeds redaction and masks it out of the very
errors that name it.

```yaml
permissions:
  contents: read
  id-token: write   # mint a short-lived GitHub OIDC token; the framework exchanges it
env:
  ANTHROPIC_FEDERATION_RULE_ID: fdrl_...
  ANTHROPIC_ORGANIZATION_ID: <org uuid>
  ANTHROPIC_SERVICE_ACCOUNT_ID: svac_...   # the id, not the name — a name is refused locally
```

A static key set in env still wins when present, so migration is: configure the rule in the
Anthropic Console, add the identifiers, watch one green run, then delete the secret.

## 3. TDD implement, triggered by a comment

`/implement` on an issue runs `implement/from-ticket` on the default branch. The strategy IS the
adapter, so making it test-driven is naming a different class in the binding — the scaffold binds
`Oneshot`, and red-then-green costs a second model phase, which is why it is a choice and not the
default.

```python
from in_lockstep.adapters.ai import TDD, Implement

lockstep.models.route("implement", "anthropic:claude-sonnet-4-6")
lockstep.bind(Implement, TDD())
```

`in-lockstep ls` prints the whole story as one line — `Implement -> TDD` — and the model comes
from the route above: an adapter bound with no explicit invoker resolves it per run, and egress
from the bound `EgressPolicy`.

This repository's own [.lockstep/lockstep.py](../.lockstep/lockstep.py) is the full worked
version — including the `WorktreeRunner` wrap that keeps a model-chosen command's writes off the
live tree.

## 4. A rolling daily spend ceiling

The per-run budget bounds one run; this bounds a runaway *trigger* — a chat-ops loop firing all
night. Summed from the ledger's own records over a rolling 24 hours, refused before a run
starts:

```bash
export IN_LOCKSTEP_DAILY_LIMIT=10.00
```

Honest scope: the window sums this clone's ledger, so a runner that never fetched
`lockstep-history` sums less than the truth. The provider console's organisation limit
(`IN_LOCKSTEP_ORG_SPEND_LIMIT`, attested to `doctor`) remains the durable backstop.

## 5. House guardrails in every review

Prompt text is data; the binding site is where data enters, visibly. `.plus` appends after the
shipped baseline, so extending cannot quietly drop it.

```python
from in_lockstep.adapters.ai import AiReview, Review
from in_lockstep.prompts.review import review_layers

lockstep.models.route("review", "anthropic:claude-sonnet-4-6")
lockstep.bind(
    Review,
    AiReview(
        layers=review_layers().plus(
            guardrails=(("house", "Do NOT propose new dependencies; flag them instead."),),
        ),
    ),
)
```

## 6. Ship your organisation's standards as a package

At one repository, a `lockstep.contribute(...)` line; at two hundred, a package — installing it
is what applies it, and `Lockstep.detect()` discovers it before your module's own lines run.
The entire org layer is a `pyproject.toml` entry point and one function:

```toml
[project.entry-points."in_lockstep.standards"]
acme = "acme_lockstep:apply"
```

```python
from in_lockstep import Policy

def apply(std):
    std.contribute(Policy(name="acme-baseline", scan_input="block", max_turns=16))
```

Everything lands at `Tier.PLUGIN`, so any repository's own `lockstep.bind` still wins, and
`in-lockstep ls` prints what applied. Worked example:
[examples/acme-standards](../examples/acme-standards/).

## 7. Feed your proxy — real egress enforcement

The framework never enforces destinations itself (an in-process allowlist would be a checkbox);
it verifies that *something outside the process* does. The manifest is the bridge:

```bash
in-lockstep egress-manifest        # the hosts a run may dial, one per line
```

Feed that list to the proxy or firewall your CI host provides, then attest it:

```bash
export IN_LOCKSTEP_EGRESS=enforced
```

`enforced` is verified, not believed — a probe to a host that must be unreachable refuses the
run if it connects. Extra hosts you decide to allow (a package registry, before an
`EXECUTES_CODE` step needs it) go in `EgressPolicy(allow=("pypi.org",))`, and the manifest
prints them.

## 8. Record a cassette once, review offline forever

Cassettes sit at the `LLMInput`/`LLMOutput` seam, so a recording replays against a different
provider, and tool IO is captured alongside model IO.

```bash
in-lockstep review --base origin/main --record       # one real call, writes the cassette
in-lockstep review --base origin/main --offline      # deterministic and free, from here on
```

The replay refuses to silently call out when the prompt no longer matches the recording — a
changed guardrail means re-recording, and it says so rather than billing you quietly.

## 9. A house review lens, bound rather than monkeypatched

A lens is a prompt class; binding it is how it becomes real — visible in `ls`, loaded from the
trusted ref, never an import-time side effect.

The body is a **file**, not a string literal — prompt text is data a non-programmer edits and a
diff reviews, which is why it lives outside the module. A string is refused at class creation
rather than at render time.

```python
from in_lockstep.adapters.ai import AiReview, Review
from in_lockstep.ai.prompt import Body
from in_lockstep.prompts.review import LENSES, ReviewPrompt

class LicenseLens(ReviewPrompt):
    aspect = "license"
    body = Body.from_path(".lockstep/prompts/license.md")

lockstep.bind(
    Review,
    AiReview(lenses={**LENSES, "license": LicenseLens}),
)
```

`in-lockstep show-prompt license` renders it offline, and `ls` stars it as not-the-shipped-prompt.

## 10. Make the ledger auditable

Every run records to the `lockstep-history` orphan branch. Two commands read it, and both check
it was not rewritten:

```bash
in-lockstep report --by model     # aggregates, and flags any record rewritten after append
in-lockstep doctor                # DOC167, an ERROR — tampering fails a required check
```

The check reads the retained chain; a force-push that *replaced* the chain is the remote's to
refuse. Protect `lockstep-history` with a ruleset blocking force-pushes and deletions — appends
are fast-forwards and still flow, so it needs no reviews and slows nothing down.
