# This repository compiles its own drift gate

`lockstep` now has a pipeline of its own, in [`.lockstep/`](../.lockstep). It compiles to exactly one
workflow — [`.github/workflows/pipeline-ci.yml`](../.github/workflows/pipeline-ci.yml) — which
recompiles the spec on every pull request and byte-compares the result against what is committed.

That is a small pipeline. The reason to write it down is that self-hosting is the one case the
compiler was not built for, and building it exposed four things that were wrong for everybody.

---

## The one thing that is different here

Every other pipeline installs a **released** compiler. That is what makes the drift gate a clean
statement: the output is a function of the spec, the compiler cannot move under a pull request, and
so recompiling and comparing tells you the spec and the output agree.

Here the compiler *is* the repository. The output is a function of the spec **and** of `src/`. Two
consequences, and both are traps:

**The gate must install the checkout.** `capabilities.compiler` is written verbatim into
`uv tool install "{compiler}"`. Left at `lockstep>=0.1,<1.0`, this repository's gate would compile
the pull request's spec with the *previous* release's compiler — testing the wrong compiler, and
passing green on a pull request that broke the emitter. So the manifest says:

```yaml
capabilities:
  compiler: "."
```

**The gate must trigger on the compiler.** `pipeline-ci.yml` fires on the spec directories, because
normally the spec is the only input. A pull request touching only `src/lockstep/emit/` would not
have run it — the change most worth recompiling would have been the one change the gate never saw.
That is what `watch:` is for:

```yaml
targets:
  github-agentic:
    watch:
      - src/**
      - pyproject.toml
      - uv.lock
```

`watch` is repository-relative, not pipeline-relative: it names things *outside* the pipeline, which
is the only reason to write it at all. It is not a self-hosting keyword — any repository that keeps
an extension outside its pipeline directory has the same second input.

---

## The circularity, and the rule it forces

The gate is compiled by the thing it gates. A `pull_request` runs the workflow files from the merge
of the pull request, so a change that breaks the emitter emits its own broken gate, and the broken
gate is what runs.

So [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) stays **hand-written, permanently**. It
runs `make ci` — 724 tests, the golden output tree, the compiler/runtime contract — and no compiler
change can rewrite it. Among those tests is
[`tests/test_selfhost.py`](../tests/test_selfhost.py), which recompiles `.lockstep/` and compares it
to what is committed. The trusted workflow checks the generated one; the generated one does not
check itself.

This is the general rule for adopting a pipeline anywhere, not a quirk of this repository: **the
pipeline is an addition to your CI, never a replacement for it.**

---

## Why this pipeline has no steps

`pipeline-ci.yml` is the only thing `.lockstep/` compiles to, and that is deliberate. Two of the
three packaging units are unpublished: `pipeline-fw/pipeline-actions` and the `pipeline-exec`
container image are both pinned to placeholders across the examples, which
`lockstep compile` says out loud on every run. A step compiles to a job with

```yaml
container: ghcr.io/pipeline-fw/pipeline-exec@sha256:0000…
```

so any pipeline with a script or builtin step in it is a pipeline that cannot run yet. `pipeline-ci`
touches none of that — `actions/checkout`, `astral-sh/setup-uv`, `uv tool install`, `lockstep` — so
it is the one generated workflow that runs today, and it is what this repository compiles.

The useful pipeline — a `/review` whose lenses know [what goes where](layers.md) and can say when
`src/lockstep/emit/` changed and `tests/golden/` did not — is the next step, and it needs the
composite actions published first.

---

## What dogfooding found

Every item here was a defect for other people that nobody had been in a position to notice.

**`doctor` demanded pins for capabilities the output never named.** A pipeline whose work is all
compiler steps pulls no container and calls no composite action, and `doctor` failed it with DOC001,
DOC002 and DOC016 anyway — three red gates with nothing behind them. `Spec.capabilities_used()` now
answers what the compiled output will actually reference, and `doctor`, `pin` and the compile notes
all ask it first.

**`DOC007` asked for a run budget on a pipeline with no agents.** A number nothing reads.

**`.gitattributes` collapsed workflows the compiler did not write.** It marked `*.yml
linguist-generated=true` in the output directory — which, ever since a pipeline could be added to a
repository that already had workflows, is somebody else's CI being hidden in every pull request
diff. Here it would have hidden `ci.yml`: the one file that must stay readable, because it is the
one no compiler change can rewrite. The compiler knows exactly which files it wrote, so it now names
them. `*.lock.yml` stays a pattern — gh-aw produces those after the compile.

**The `scripts` job did not trigger on the tests it runs.** `SPEC_PATHS` listed `scripts/**` and not
`tests/**`, so a pull request that broke a script test by editing the test ran no gate at all.

**`lockstep show-surface` called an undeclared capability `UNPINNED`.** Same mistake in a fifth
place: it now says `(unused)`, which is what it is.

---

## Reproducing it

```bash
lockstep compile --check    # what the gate runs, from the repository root
lockstep lint
lockstep doctor
```

All three are clean, and `.lockstep/` is four files: a manifest, a profile, the lock, and the compile
manifest.
