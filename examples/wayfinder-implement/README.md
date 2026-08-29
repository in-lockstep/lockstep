# Implement, charted before it is built

An `Implement` strategy modelled on [Matt Pocock's wayfinder skill](https://github.com/mattpocock/skills/tree/main/skills/engineering/wayfinder).

> **The framework has since grown its own implement verb** — `AiImplement` and the
> `implement/oneshot` strategy, which reads a ticket and stages a change. This example is a
> *different* answer to the same verb, and the contrast is the point: `oneshot` is handed a ticket
> and builds it, while wayfinder refuses to build anything until the map says the ticket is
> claimable. Both bind `Implement`; neither knows the other exists. That is what a verb interface
> being a marker class buys you.

## What wayfinder is

Wayfinder is a planning skill for **large, uncertain efforts** — work where the honest answer to
"what needs doing?" is *we don't know yet*. Rather than trying to execute a foggy scope, it builds
a shared decision map on an issue tracker and resolves it a ticket at a time.

Two kinds of session:

**Chart the map.** Name the destination through conversation, explore the frontier breadth-first,
create a map issue with a Destination, Notes and fog, spawn child tickets, wire the blocking
relationships, and *stop*. Charting only — nothing gets built.

**Work through the map.** Load the map's low-resolution view, claim one unblocked frontier ticket
by self-assignment, resolve it, record the resolution, close it, update Decisions-so-far, and
graduate whatever fog has become specifiable into fresh tickets. Repeat until the route is clear.

Its constraints are what make it work:

- **One ticket per session.** A decision should be attributable to the run that made it.
- **Refer to tickets by name**, never a bare ID.
- **Plan, don't deliver.** A charting session produces decisions, not deliverables.
- **Fog remains foggy.** Only ticket what is sharp enough to phrase precisely *now*.
- **Native blocking.** Use the tracker's own dependency graph so the frontier is visible.

The original skill drives an agent with prose. This example does something narrower and, for a
framework, more useful: it turns the checkable half of those constraints into code.

## What this example does

`in-lockstep` ships no `Implement` adapter at all, so this is what extending a verb actually looks
like from outside the framework — not configuration, but a small amount of ordinary Python.

```
wayfinder.py    the verb interfaces, the spec, and two adapters
github_map.py   builds a map out of labelled GitHub issues
lockstep.py     the whole configuration: two bindings, a budget, a policy floor
map.json        a sample map, for running it without a tracker
```

### The configuration lives in `.lockstep/`

`.lockstep/lockstep.py`, not the repository root. The root is on `sys.path` for anything run from
there, so a module named `lockstep` sitting in it is importable by the project whether or not
anyone meant it to be — which is how framework types leak into code that never chose to depend on
them. A dot-directory is not a valid package name, so nothing under it is importable by accident.

The example's own modules (`wayfinder.py`, `github_map.py`) sit *beside* `.lockstep/` rather than
inside it, because they are the project being configured rather than the configuration.

### A verb interface is a marker class

```python
class Implement:
    """Workflows ask for this; a binding decides what serves it."""
```

That is the entire mechanism. `ctx.do(Implement, spec)` resolves whatever is bound, so *which
strategy runs* is a binding decision and a workflow never has to know.

### Charting gets a verb of its own

```python
CHART = Verb("chart")
```

`Verb` is open, so a genuinely new activity gets a genuinely new name. That matters more than it
looks: charting writes nothing, spends nothing and succeeds by producing decisions. Filing it under
`implement` would put both wayfinder sessions under one heading in every span, metric and step id —
and telling those two runs apart is most of the point of keeping a map.

`in-lockstep ls` prints any verb that is defined and unbound, which is how a typo surfaces. Writing
this example tripped that warning, which is why there are two bindings rather than one adapter with
a mode flag.

### Four constraints become checks, not prompt text

| Wayfinder rule | How it is enforced here |
|---|---|
| Claim only frontier tickets | `Outcome.blocked_by("wayfinder.not_on_frontier")`, naming what blocks it |
| One ticket per session | A number the adapter refuses to exceed |
| Refer by name | An unknown key is `wayfinder.unknown_ticket`, not a guess |
| Plan, don't deliver | Charting returns no change, and the policy floor denies every write tool |

A rule stated only in a prompt is a request. This framework's whole argument is that the
difference matters — so the rules that *can* be checked are checked, and the model is left to do
the part that genuinely needs judgement.

### Fog is read, not guessed

```python
def _is_fog(ticket):
    return not ticket.acceptance_criteria and not ticket.description.strip()
```

A ticket with no criteria and no description has not been specified, whatever its title promises.

### "Plan, don't deliver" survives the model

```python
lockstep.contribute(
    Policy(
        name="wayfinder-floor",
        deny_tools=("write_file", "delete_file", "shell", "apply_patch"),
        scan_input="block",
    )
)
```

Denied tools are removed from the dispatch table rather than refused when called — `ToolSet` *is*
the table, so there is nothing to reach. A charting session that *could* write files would
eventually write some, whatever the prompt said.

`scan_input="block"` matters because ticket text is untrusted: anyone who can file a ticket can
write into a prompt. `Ticket.as_context()` already tags it `UNTRUSTED_EXTERNAL`.

## Running it

Everything below is free. Charting is deterministic, so it needs no API key and spends nothing.

```bash
cd examples/wayfinder-implement
uv run --project ../.. in-lockstep ls          # what is bound
```

### Against the sample map

`map.json` is a four-ticket map you can edit.

```bash
uv run --project ../.. in-lockstep run wayfinder/chart
uv run --project ../.. in-lockstep run wayfinder/work --arg target=MAP-1
```

| | |
|---|---|
| `chart` | `succeeded`, `decided=True`, `$0.0000`, frontier and fog reported |
| `work MAP-1` | `blocked (wayfinder.not_on_frontier)` — exit 3 |
| `work NOPE` | `blocked (wayfinder.unknown_ticket)` |

Note `MAP-4` is closed and therefore absent from `MAP-1`'s blockers. A closed blocker no longer
blocks, or the frontier never advances and the map is just a list.

### Against real GitHub issues

Needs `gh` authenticated, and nothing else.

```bash
gh label create wayfinder --description "On the wayfinder map"
gh issue edit 53 --add-label wayfinder      # and any others on the map
```

Blocking is a convention in the issue body, because GitHub has no general "blocked by" between
issues:

```
Blocked by: #52, #51
```

One greppable line, visible to whoever reads the issue — which matters more than elegance, since
a frontier is only useful if the people working from it see the same graph the framework does.

```bash
uv run --project ../.. in-lockstep run wayfinder/chart-github --arg target=#53
```

```
wayfinder/chart-github  succeeded
  wayfinder.frontier: claimable now: #52, #51
  wayfinder.destination: #53 is blocked by #52, #51
```

And claiming behind the frontier is refused, with exit code 3:

```bash
uv run --project ../.. in-lockstep run wayfinder/work-github --arg target=#53
```

Acceptance criteria are read from task-list lines (`- [ ]`), which is how issues carry them in
practice — and their absence is what marks a ticket as fog.

### Working a ticket

Working needs a model, so `WayfinderImplement` takes an `invoker_factory` and refuses with
`wayfinder.no_invoker` when it has none, rather than pretending to work and returning nothing. To
wire one, copy the invoker construction from `review_cmd` in `src/in_lockstep/cli.py` and pass it
to the binding in `lockstep.py`.

`tests/in_lockstep/test_example_wayfinder.py` exercises all of it. An example nothing runs is
documentation that compiles.

## Why `Outcome` makes this expressible

Charting returns a run that **succeeded**, **decided something**, and **wrote nothing**.

That combination is why `decided` is a field separate from `status` rather than a seventh status
member. Without it, a framework has to report a charting session as either a failure (it built
nothing) or a no-op (nothing happened) — and it is neither. It is the successful completion of the
work wayfinder says to do first.

## Where the run record goes

Nowhere in the working tree. Every run appends a commit to the orphan branch `lockstep-history`,
which shares no commit with any branch you work on:

```bash
in-lockstep history            # what this checkout has recorded
in-lockstep history --push     # publish it; needs push access, never automatic
```

A charting session is exactly the kind of run worth keeping: it produced decisions and no change,
so the record IS the output.

## One thing this example does not do

It names a strategy id, `implement/wayfinder`, but does **not** rely on `StrategyRegistry` to
select it. That registry is a catalogue at 1.0, not a dispatcher — nothing selects from it yet —
so the `bind()` calls are what actually decide behaviour.

Said here rather than left for you to discover after registering into it.

## Credit

The wayfinder skill is by [Matt Pocock](https://github.com/mattpocock), MIT-licensed, from
[mattpocock/skills](https://github.com/mattpocock/skills). The concepts — charting versus working,
the frontier, fog, one ticket per session, plan-don't-deliver — are his. This example is an
independent implementation of the checkable parts against `in-lockstep`'s verb model; it is not a
port of the skill, and it does not vendor any of its text.
