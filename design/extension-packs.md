# Extension packs and the index

**Status:** Implemented · **Describes:** `main` @ a9c0b9f · **Landed across** #118, #119, #121,
#122, #123, #124, from the proposal in #117.

An extension pack is an ordinary Python distribution that offers a prompt, a strategy or a verb to
any repository that installs it. A catalog is a static `index.toml` in a git repository listing
packs and pointing at receipts committed beside them. Between the two, an extension one team writes
can travel to another — and a repository can find out what one would do to it before installing,
measure it before trusting it, and be told when it changes.

The whole design turns on one sentence: **installing a pack offers it, and a line somebody wrote is
what puts it in force.** Everything below is either that property or a consequence of it.

This document described a proposal when it was written. It describes the code now. Where the
implementation departed from the proposal — three places — it says so, because a design note that
quietly rewrites its own history is worth less than the argument it lost.

---

## 1. Where a pack lands in the resolution order

Four sources may write a decision. They are applied in one order and win in the opposite one, and a
distribution mechanism is a claim about where in this stack a stranger's code may land.

| Applied | Source | Tier | May set |
|---|---|---|---|
| 1st | shipped defaults | `SHIPPED` | bodies, layer stacks, `LENSES`, default policy |
| 2nd | installed `in_lockstep.standards` packages | `PLUGIN` | tighten-only policy layers, plugin-tier bindings |
| 3rd | `.lockstep/lockstep.py` | `EXPLICIT` | anything; outranks every plugin regardless of order |
| 4th | the call site — `ctx.do(..., via=tdd)` | call-scoped | one run only; the same capability-keyed gates apply |

Policy is the exception and stays the exception: layers merge tighten-only, so order decides who is
printed, not who wins.

**A pack writes into band three and nowhere else.** There is no fifth band, and nothing lets a third
party land at `Tier.EXPLICIT` on its own — a pack that is installed and not named in `lockstep.py`
has no effect whatsoever, which `GATE-PACK-1` holds.

---

## 2. The three extension surfaces

These are what an adopter extends. They are ordinary subclassing and binding, and packaging one for
distribution (§3) changes nothing about how it is written.

### 2.1 Prompts

Subclass a shipped prompt and set `emphasis`, or point `body` at your own `.md`. Install it by
spreading `LENSES` into `AiReview(lenses=…)`. House guardrails append via
`review_layers().plus(…)`, which cannot displace the shipped baseline.

What you write is now visible before it runs:

```bash
in-lockstep show-prompt security            # what a run would send, off the BOUND adapter
in-lockstep show-prompt security --diff     # what you changed, against the shipped body
in-lockstep show-prompt security --shipped  # the framework's version, ignoring your binding
```

That was #118, and it was a defect rather than a feature request: `show-prompt` imported the
shipped `LENSES`, `PROMPTS` and `TRIAGE_PROMPTS` maps and never loaded the module, so the one
command whose job is answering *what was the model actually told* showed a team the prompt they had
replaced. Three prompt families — `fix`, `rfe`, `backport` — shipped and were unreachable from it
entirely.

`ls` prints the same answer in summary: each AI binding's prompts, starred where the class is not
the shipped one, with the guardrail chain underneath.

### 2.2 Strategies

Subclass `ImplementStrategy` or `FixStrategy`, implement `invoke`, and `lockstep.use(Yours)`.
`capabilities` is refused at class creation if it declares less than the strategy holds, and `use()`
fills in the `WorktreeRunner` wrap and the `InvokePolicy.under(...)` floor that a hand-written
`bind` can silently drop.

### 2.3 Verbs

`Verb("benchmark")` is first-class: its own telemetry label, step ids, spend accounting, middleware,
kill switch. Everything keyed on the *adapter* comes free. Five things keyed on the *verb* are owed
by the author — a strategy and request type, a route and a price, prompts and a layer stack, a
corpus family, and a `@workflow` (there is deliberately no CLI subcommand for a custom verb). A
verb pack is that set, shipped together.

---

## 3. Packs

A pack is an ordinary Python distribution. There is no manifest format for behaviour, because the
container is still the registration mechanism.

```
acme_review_prompts/
├── __init__.py            # a docstring, for a data pack
├── pack.toml              # kind and summary; nothing that changes what runs
├── prompts/*.md
├── corpus/<family>/<prompt>/*.json
└── cassettes/*.json
```

Resources live *inside* the module directory rather than at the distribution root, because files
outside the package are not installed by a wheel without extra declaration.

### 3.1 Two entry-point groups, because they answer different questions

```
in_lockstep.standards      installing IS applying
  package ──▶ detect() ──▶ container        (Tier.PLUGIN, tighten-only)

in_lockstep.extensions     installing only OFFERS
  package ──╳── container                   never binds itself
     └─────▶ a line in lockstep.py ─────▶ container
```

The older group is right to apply itself: a standards package can only tighten, so the real risk is
a repository forgetting one. An extension pack is the opposite risk — it hands a model write and
execute tools and it spends money — so its arrival is a diff somebody read. `detect()` does not even
query the extensions group.

That is the same structural property #104 and #116 established from the other direction: which
strategy runs is a bind-time code decision, so nothing a ticket carries can steer a run. A registry
resolving `--strategy acme/tdd-pro` over installed packages would have handed it back.

Two consequences worth stating:

**Listing a pack runs no code it ships.** `installed()` never calls `entry.load()`, and `pack.toml`,
the corpus, the cassettes and the AST behind `imports` are all read through distribution metadata
rather than `importlib.resources`, which would import the package to answer.

**A broken pack is quiet, where a broken standard is loud.** `load_standards` raises, because
running without standards somebody installed is a control silently absent. A pack that fails to
parse has applied nothing, so it is listed beside the others rather than hidden behind an exception.

### 3.2 The receipt: derived from code, never claimed in a file

`in-lockstep pack describe` derives what a configuration does. Every field is read off something
that already declares it:

| Field | Read off |
|---|---|
| `capabilities` | the bound object — the same frozenset `ApprovalGate`, the budget refusal and `Retry` key on |
| `projection` / `guardrails_intact` | the composed prompt, via `PromptLayers.projection()` |
| `policy` | the stack's own layers with their sources, plus what they merged to |
| `models[].priced` | the same `table_for` `DOC151` uses |
| `egress` | the same manifest `egress-manifest` hands the proxy |
| `imports` | an AST walk over every `.py` the distribution ships |
| `offers` | the imported namespace, through `core`'s vocabulary — `verb` plus `capabilities` |
| `corpus` / `cassettes` | files on disk |

Two subjects. With no argument the subject is **this repository**, which is where the format was
exercised first and is useful on its own as an audit of your own configuration. With a name it is an
**installed pack**.

`--json` is canonical: sorted keys, and the digest is over exactly what is printed, **excluding
itself** — a receipt that hashed its own hash could not be verified by recomputation, which is the
only thing the digest is for.

Two fields carry the argument. `guardrails_intact` says whether a prompt still opens with
`guardrail:baseline` — legal to change, and the thing a reader of somebody else's extension most
needs told. `corpus` is `null` rather than `0` when a repository has none, and says in words that
nothing bound there has been measured; borrowing the framework's shipped case count would be the
reassuring number computed from somebody else's evidence. `priced` gets three states for the same
reason: `true`, `false`, and `null` when the provider is not registered, because a machine that
cannot check a route is not a repository with a broken one.

### 3.3 `imports`, and why it is a field rather than a tier

`none`, `modules`, or `unknown`, derived by walking the AST: `none` means every module the pack
ships holds a docstring and nothing else, so importing it can run no code of the pack's own.

A **tier** — `kind = "prompt"` refuses any real Python — was considered and rejected. It buys a
category-wide guarantee and immediately meets its awkward case: the pack that wants to ship the
three-line `Prompt` subclass binding its own body, which is the natural thing for a prompt author to
include and which makes the pack code. Then either the tier lies or the pack is not a prompt pack.

So `describe` computes the field for *every* pack, and the rule bites at one boundary: the project's
catalog will not list a pack as `kind = "prompt"` unless it reports `imports: none`.

`unknown` is neither of the others. A distribution nothing could resolve to files has not been
checked, and reporting it as inert would be the reassuring answer computed from nothing.

`pack.toml` carries a kind and a summary. An unknown key is **refused**, not ignored: a key silently
accepted arrives, gets documented, and becomes load-bearing — which is how a declaration file turns
into a configuration language. The kind is cross-checked against what `describe` derived, so a pack
calling itself prose while shipping a strategy says so in its own receipt.

---

## 4. Accepting one: `add` prints, and does not write

```
$ in-lockstep add acme-tdd-pro

  pack          acme-tdd-pro 2.1.0
  imports       modules  (importable code — installing puts it in your import graph)
  capabilities  reads_repo spends_budget writes_files executes_code
  catalog       the installed code matches the published receipt

  recorded      .lockstep/packs/acme-tdd-pro.json
                commit it: the record IS the acknowledgement, and doctor reads it.

  paste into .lockstep/lockstep.py:

      from acme_tdd_pro import AcmeTDD
      implement = lockstep.use(AcmeTDD)

  until you do, nothing changes: `in-lockstep ls` will not mention acme-tdd-pro.
```

**It never edits `.lockstep/lockstep.py`**, under any flag, which `GATE-PACK-4` holds. That file can
rebind any adapter, remove any middleware and grant any tool; it is the first entry in its own
protected-path deny list and it loads from a trusted ref precisely because every line in it was
typed by a person. The failure mode is good too: a pack installed and unbound does nothing at all,
and `ls` says so, which is a better state than a binding nobody noticed arriving.

**It does not install anything either — a departure from the proposal.** The first draft had `add`
pin the dependency itself. Putting a stranger's code on your machine is your package manager's job,
in your dependency diff, and a framework that ran the installer would be the thing deciding what you
trust rather than the thing telling you what you are trusting. `add` requires the pack to be
installed, reports whether it is pinned, and prints `uv add <distribution>` when it is not.

### 4.1 The acknowledgement is a committed file

`.lockstep/packs/<name>.json` holds the receipt as it stood when the repository accepted it. This
replaced the proposal's `lockstep.use(AcmeTDD, acknowledged="2.4.0")`: a version string in code says
*when* something was accepted and not *what*, and the comparison that matters is over capabilities,
offers and the projection. It also keeps bookkeeping a tool maintains out of the file whose value is
that a person wrote it — the same instinct as printing the bind lines.

**Widening is the line.** A capability the pack did not previously hold is refused until `--accept`
says otherwise, and a refusal records nothing, so it cannot leave a repository having accepted what
it declined (`GATE-PACK-3`). Everything else — a version, a new prompt, a corpus that grew — is
recorded without a flag and reported. Refusing over those too would teach people to pass `--accept`
by reflex, which is how the flag that guards agency stops meaning anything.

Comparison runs over a **material** subset of the receipt, excluding `requires` and `imported`: a
receipt published on one framework version compared against one derived on another differs in the
deriving machine and in nothing that matters, and a comparison that called that drift would cry wolf
on every upgrade until people stopped reading it.

### 4.2 What upgrades have to say out loud

```
$ in-lockstep doctor

  DOC170  pack 'acme-tdd-pro' may now do more than this repository accepted: +reaches_network
          Read what changed, then accept it in a diff:
          `in-lockstep add acme-tdd-pro --accept`, and commit the record.       FAIL

  DOC171  review/security does not open with the shipped guardrail baseline     WARN
  DOC172  pack 'acme-tdd-pro' is installed but not pinned                       WARN
```

Only `DOC170` is an error, and the line it draws is agency. It is a **note** rather than a failure
for a pack installed and never accepted — installing offers a pack, so one that nothing binds is
ordinary, and failing over it would make the group's premise read as a problem. `DOC171` reads the
**bound** adapters rather than the installed packs, because what matters is the prompt a run would
actually send.

---

## 5. The catalog

A static file in a git repository. No service, no accounts, no ranking to defend.

```toml
# index.toml
[index]
criteria = true          # this catalog claims to apply the criteria in §5.1

[[pack]]
name         = "acme-review-prompts"
distribution = "acme-review-prompts"
index        = "https://pypi.org/simple"
kind         = "prompt"
summary      = "A security lens that knows about our ORM"
source       = "https://github.com/acme/review-prompts"
receipt      = "receipts/acme-review-prompts-1.0.0.json"   # derived, not written
```

```bash
in-lockstep market add acme https://raw.githubusercontent.com/acme/index/main/index.toml
in-lockstep search tdd
in-lockstep market lint index.toml      # for whoever publishes one, in their CI
```

`market add` writes `.lockstep/market.toml`, committed, because registering a source decides where
this repository looks for code. **https only** — a catalog says what to install, so over plain http
that description is whatever the network says it is, and the receipt comparison below would be
checking an attacker's document. An entry may not carry a key that configures, the refusal
`pack.toml` already makes. An entry's `receipt` path may not leave the repository: it is untrusted
input naming a file this process opens.

`search` groups by source and reports a name two catalogs claim rather than resolving it. Guessing
which one somebody meant is how the wrong code gets installed under the right name.

**The catalog is an install-time artifact.** Nothing reads it during a run, so a run of a repository
that installed a pack is identical to a run of one that vendored the same class by hand.

**Its receipts are falsifiable.** An entry points at a receipt derived by `pack describe`, so it
records what the author's code did rather than what the author wrote. `add` re-derives it and
refuses a pack that holds more than the catalog published — outright, not behind `--accept`, because
that is not a decision to weigh, it is a listing that does not describe the code.

### 5.1 Entry criteria, not endorsement

The project's own catalog is neither curated nor a bare tap. It states criteria, all machine-checked
off the receipt:

1. a receipt at all, derived by `pack describe`;
2. a pack listed as `kind = "prompt"` reports `imports: none`;
3. a corpus, so the pack can be measured;
4. at least one cassette, so measuring it costs nothing.

**A criterion the proposal listed is not among them.** *The layer projection retains
`guardrail:baseline`* cannot be answered from a pack's receipt: a prompt body has no projection
until something composes it, and which guardrails end up around it is a property of the repository
that binds it. `DOC171` is where that question is answerable, and it is asked there against the bound
adapters. The same correction narrowed `GATE-PACK-2`.

Criteria 3 and 4 are two entries because they are two things: a corpus says what to measure, a
cassette says measuring costs nothing, and a pack with cases and no recording can only be measured by
somebody who pays.

The wording is load-bearing. Meeting these says a pack keeps the framework's guardrails and can be
measured before it is trusted. It says nothing whatever about whether the code is good, and a catalog
implying otherwise would transfer a judgement nobody made — the distinction `evaluation/` already
insists on when it reports an unjudged rubric as outstanding rather than as a pass.

An organisation's internal tap carries no criteria, because it answers a different question: an
internal pack is trusted by the fact that someone inside the company published it.

---

## 6. Measuring one: `pack try`

```
$ in-lockstep pack try acme-tdd-pro --corpus ./our-cases

  trial         acme-tdd-pro  (replaying — no key, no spend)
  cassettes     1  (review)

  its own cases  (14)
    decided     9     pass rate 0.89
    outstanding 5   — need a judge, so not passes

  your cases  (22)
    decided     17    pass rate 0.76
    unrecorded  5   — no recorded exchange; not failures
```

Everything else about a pack can be checked and none of it says whether the pack is any *good*. The
answer to that is a measurement made on your own cases, which is why `--corpus` exists and why its
results are counted apart.

Four states, and keeping them distinct is the point:

| State | Means | Feeds the rate |
|---|---|---|
| `decided` | a machine settled it | yes |
| `outstanding` | a rubric, and no judge has answered | no |
| `unrecorded` | the cassette holds no answer for this case | no — absence of evidence, not a failure |
| `unexercised` | a corpus family a trial cannot drive | no — counted, never dropped |

Counting `unrecorded` against a pack would penalise an author's incomplete recording rather than
their code. Dropping `unexercised` silently would report a pass rate over a corpus the trial never
saw. When nothing was decided there is no pass rate, and the output says which absence produced that
rather than printing a zero.

A trial composes the pack's `prompts/<aspect>.md` inside the **shipped** layer stack, paired by
convention with `corpus/review/<aspect>-reviewer/`. A repository's own guardrails are deliberately
not applied: measuring a pack through your configuration measures your configuration, and two
repositories would get different numbers for the same pack with no way to tell why.

**Somebody has to pay once.** `pack try --record` calls the provider and writes the cassette everyone
after replays for nothing. Recording transmits and obeys the same egress rules as any real call;
replaying transmits nothing and `AiInvoker.transmits` already knew the difference, so no control
needed an exemption for measurement.

### 6.1 The honest limit

A $0 trial ranks on **deterministic cases only**. Rubrics need a judge and a judge costs money. This
step did not resolve that; it made it visible — a pack whose corpus is mostly rubrics shows a small
`decided` beside a large `outstanding` on every run, which is the true shape of its evidence.

---

## 7. Three refusals

**No request-time selection.** There is no `--strategy acme/tdd-pro` resolving through a catalog.
Which strategy runs stays a bind-time code decision. The CLI's existing `--strategy tdd` remains what
it was: a shipped-only convenience for a repository that has bound nothing.

**No remote prompt bodies at run time.** A pack's markdown is vendored into the environment at
install and read through `importlib.resources`, like every shipped body. A prompt fetched during a
run is an ungoverned input to a model holding write tools.

**No pack-supplied middleware, and no pack policy that loosens.** Extension packs offer classes and
resources and may not contribute policy at all. A pack that wants to constrain a repository is a
standards package, where contributions merge tighten-only and print with their source.

---

## 8. The surface, in one place

| Command | Answers |
|---|---|
| `show-prompt <name> [--diff\|--shipped\|--projection]` | what the model is told, off the bound adapter |
| `ls` | what is bound, and what each AI binding composes |
| `pack ls` | which packs are installed — offered, not in force |
| `pack describe [<name>] [--json\|--no-load]` | the receipt, for this repository or a pack |
| `pack try <name> [--corpus\|--record\|--json]` | what it scores, on its cases and yours |
| `add <name> [--accept]` | accept it, record what was accepted, print the lines |
| `market add\|ls\|lint` | which catalogs this repository reads; whether one meets its own criteria |
| `search <query>` | packs across those catalogs, grouped by source |
| `doctor` | `DOC170` widened capabilities · `DOC171` displaced baseline · `DOC172` unpinned |

Public API: `in_lockstep.packs` (`pack`, `installed`, `Pack`, `pinning`),
`in_lockstep.receipt` (`receipt_for`, `receipt_for_pack`, `compare`, `read_record`, `write_record`),
`in_lockstep.market` (`sources`, `read_catalog`, `criteria_failures`), `in_lockstep.trial`
(`run`, `Trial`). Worked examples: [`examples/acme-review-prompts/`](../examples/acme-review-prompts/)
and [`examples/lockstep-index/`](../examples/lockstep-index/).

---

## 9. What this changed in `in-lockstep-design.md`

Two sections of the master design document described shapes the code does not have, and one of them
would have led somebody to rebuild what §3.1 refuses. They are amended in **§18** of that document,
following the idiom §17 established — an "amends §X" delta rather than an edit in place, so what was
designed and what shipped are both readable.

- **§18.1 *(amends §5.7)*** — the strategy protocol, the registry of named approaches, and
  selection by string. The `StrategySelector` over ticket labels is withdrawn rather than deferred:
  a strategy chosen from ticket text is one an untrusted author can steer.
- **§18.2 *(amends §12)*** — the three entry-point groups that were never built, and the one that
  was. `in_lockstep.adapters`, `in_lockstep.workflows` and `in_lockstep.evaluators` should be struck
  rather than built, for the reason in §3.1.
- **§18.3 *(amends §12)*** — three further §12 claims found unbuilt while checking the first two:
  the import-purity lint (still wanted), the `in_lockstep.x.*` incubation namespace (not wanted as
  stated), and the `.sync` mirror (wanted only when somebody needs it). Recorded rather than quietly
  dropped, because a design document asserting a mechanism nobody built is the decay
  `docs/controls-crosswalk.md` exists to catch.

## 10. Gates and checks

`GATE-PACK-1` through `GATE-PACK-4` live in [`design/gates.md`](gates.md#packs) with their
assertions and `held` status, each discharged by a test that names it. They are not restated here: a
gate recorded in two places drifts in one of them.

`DOC170`–`DOC172` are in `doctor`, described in §4.2.

---

## 11. What is not there yet

**Nothing has been dogfooded.** No pack has been installed from a real index. The worked example is
exercised by tests and has never been `uv add`-ed, and no catalog has been published at a real URL.
This is the same gap `CONTRIBUTING.md` records about GitLab, and the first person to publish a pack
for real will find things these tests cannot.

**`examples/acme-review-prompts` cannot be measured.** It ships prose and two corpus cases and no
cassette, so `market lint` fails it on criterion 4 and `pack try` says there is nothing to replay.
That is deliberate and documented in both places: fabricating a recorded model exchange to make a
lint green would be inventing the evidence this project exists to refuse to invent. Clearing it is
one real `--record` run against a real change.

**`pack try` drives one verb.** `review` only — a single model turn with the diff in the prompt,
which is exactly what a cassette replays. `implement` and `fix` interleave tool calls whose results a
cassette also holds, so extending it is reachable and is more work than what landed.

**Open: should a catalog rank on decided *count* as well as pass rate?** A pack with one decided case
at 1.00 currently reads better than one with forty at 0.90, and nothing in the criteria distinguishes
them.

---

## Appendix: the order it landed in, and why that order

Kept because the ordering encodes an argument, not just a schedule: the first two steps are worth
having even if the rest is never built, and nothing before step 4 requires deciding what a
marketplace is.

| | Step | PR |
|---|---|---|
| 1 | Make an override visible — `show-prompt` off the bound adapter, `--diff`, the `ls` block | [#118](https://github.com/in-lockstep/lockstep/pull/118) |
| 2 | Derive the receipt — against your own repository first | [#119](https://github.com/in-lockstep/lockstep/pull/119) |
| 3 | Name the container — the pack layout, `pack.toml`, the entry point that offers | [#121](https://github.com/in-lockstep/lockstep/pull/121) |
| 4 | Make installing a diff — `add`, the accepted record, `DOC170`–`DOC172` | [#122](https://github.com/in-lockstep/lockstep/pull/122) |
| 5 | Publish the catalog — `index.toml`, `market`, `search` | [#123](https://github.com/in-lockstep/lockstep/pull/123) |
| 6 | Rank by evidence — `pack try` | [#124](https://github.com/in-lockstep/lockstep/pull/124) |

Three decisions were taken while drafting and all three survived implementation: installing prints
rather than writes (§4), `imports` is a derived fact rather than a tier (§3.3), and the project's
catalog states machine-checked entry criteria that are explicitly not an endorsement (§5.1).
