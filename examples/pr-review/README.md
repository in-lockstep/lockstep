# Reviewing a pull request

One file. `lockstep.py` in this directory is the entire configuration — no spec tree, no
manifest, nothing generated.

```bash
in-lockstep review --base origin/main --head HEAD --aspect security
in-lockstep show-prompt security          # what the model is actually told, offline
in-lockstep ls                            # what will actually run
```

## What it demonstrates

**An aspect is an agent, not a data row.** Four lenses are four registered strategies rather than
one prompt with a parameter. That is what lets each be budgeted, measured and overridden on its
own — a team that wants a house security lens subclasses one prompt and registers it, and the
other three are untouched.

**Policy tightens, never loosens.** The `review-floor` contribution denies write tools and sets
`scan_input="block"`. Contributions merge monotonically: a later one cannot widen it, ceilings
take the lowest rather than the last, and there is no removal API. What that preserves is that
taking a standard away is a visible diff — not that it is impossible.

**Untrusted context drives the controls.** The diff being reviewed is authored by whoever opened
the change, so it is tagged `UNTRUSTED_EXTERNAL`. That tag is what makes egress enforcement
mandatory for this run, delimits the diff in the rendered prompt, and sends it through the
injection scanner before the model sees it — a read-only review of a fork's diff is precisely the
case a capability-only rule would exempt.

## Running it without a key

```bash
in-lockstep review --dry-run --base origin/main --head HEAD
in-lockstep review --offline --base origin/main --head HEAD   # from a cassette
```

Cassettes sit at the `LLMInput`/`LLMOutput` seam, so one recorded against Anthropic replays
against any provider, and they record tool IO as well as model IO.
