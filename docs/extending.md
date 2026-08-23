# Extending the framework

The framework ships the mechanics that every pipeline needs — fan-out, caching, artifacts, gates —
and deliberately stops there. It does not know about your issue tracker, your test runner, or how
your repository expects a patch to land. Those are extensions, and there are exactly two kinds.

| You need | Write | Because |
|---|---|---|
| Logic that is shell, Python, or an API call | a **builtin** in a `pipeline-exec` extension | it can be unit tested, and it runs identically everywhere |
| Something that must compose *other actions* | a **composite action** | only an action can `uses:` another action |

Anything else — reading a file, transforming JSON, calling your own API — is a `script:` step and
needs no extension at all. Reach for one of these only when a script genuinely cannot do the job.

This guide builds both, in service of a real pipeline: **fetch triaged bugs from Jira, analyze them
against the application source, write reproducers, write fixes, validate them, review them, and open
pull requests.** The finished example is [`examples/bug-fix`](../examples/bug-fix), and every command
below was run against it.

---

## The pipeline we're building

```
fetch bugs ─▶ analyze ─▶ reproducer ─▶ prove it fails ─▶ write fix ─▶ apply ─▶ prove it passes ─▶ review ─▶ PR
 (builtin)    (agent)     (agent)        (builtin)        (agent)    (builtin)    (builtin)      (agent)  (action)
```

Eleven jobs. Four agents, **none of which can write anything**. One job at the end holds the only
write permission in the pipeline. That shape isn't incidental — it's the reason a pipeline that
writes code to your repository is a defensible idea rather than a reckless one.

Three of those steps need code the framework will never ship:

- **`jira-fetch`** — talks to your issue tracker.
- **`apply-patch`** — decides whether an agent's diff may land. This is a trust boundary.
- **`run-suite`** — runs the target project's own tests and turns the result into a verdict.

And one step needs a composite action: checking out a *second* repository (the application being
fixed) and installing its toolchain, which requires `actions/checkout` and `actions/setup-python` —
things a script cannot call.

---

## Part 1 — Extending `pipeline-exec` with builtins

### The mechanism

`pipeline-exec` discovers commands through Python entry points. Any installed package advertising
the `pipeline_exec.commands` group contributes commands to the CLI:

```toml
# examples/bug-fix/extensions/pyproject.toml
[project]
name = "bugfix-ext"
dependencies = ["click>=8.1", "httpx>=0.27"]

[project.entry-points."pipeline_exec.commands"]
jira-fetch = "bugfix_ext.commands:jira_fetch"
apply-patch = "bugfix_ext.commands:apply_patch"
run-suite = "bugfix_ext.commands:run_suite"
```

Each value is a `click.Command`. The name on the left is what a `builtin:` step writes in the spec —
**the spec never names your package**, so you can rename or re-home the extension without touching
any pipeline that uses it.

Install it and the commands appear:

```console
$ pipeline-exec list-commands
apply-patch     extension
cache-key       built-in
jira-fetch      extension
run-suite       extension
test-runner     built-in
…
```

Two rules the loader enforces, both loudly:

- **An extension cannot shadow a built-in command.** If you name yours `report`, loading fails and
  tells you to rename it. Silent shadowing would mean a spec that reads one way and runs another.
- **A broken extension names itself.** An import error or a non-`Command` object raises with the
  entry-point name in the message, rather than the command simply being absent.

### Writing a command

A command is ordinary Click. The one convention worth copying is how outputs are published:

```python
def _emit_output(name: str, value: str) -> None:
    """Publish a step output the same way pipeline-exec does, so callers cannot tell the difference."""
    destination = os.environ.get("GITHUB_OUTPUT")
    if destination:
        with open(destination, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    else:
        click.echo(f"{name}={value}")
```

Write to `$GITHUB_OUTPUT` when it exists and stdout otherwise. The generated workflow then carries no
shell redirection, and the same command is usable by hand.

### Builtin 1 — `jira-fetch`, producing a work list

The important part isn't the HTTP; it's the shape of what comes out:

```python
work = [
    {
        "key": issue["key"],                    # fanout keys on this: matrix legs AND filenames
        "summary": issue["fields"].get("summary", ""),
        "description": (issue["fields"].get("description") or "")[:6000],
        "priority": (issue["fields"].get("priority") or {}).get("name", "unknown"),
        "components": [c.get("name", "") for c in issue["fields"].get("components", [])],
    }
    for issue in issues[:limit]
]
```

`key` is what `fanout` keys on, so it decides both the matrix legs and the per-item output filenames.
Everything else is there so the analysing agent needs no tools to understand the bug.

That truncation of `description` is not incidental. Matrix values travel through workflow
expressions, and a bug report with a 200KB stack trace pasted into it will break the run in a way
that's tedious to diagnose. Cap it where you produce it.

### Builtin 2 — `apply-patch`, a trust boundary

This is the one that matters. An agent proposes a diff; this decides whether it lands. That decision
belongs in code — reviewable, testable, unchangeable by a prompt — and never in a model's judgement.

```python
PROTECTED = (
    ".github/",       # CI configuration
    ".pipeline/",     # pins and provenance
    "commands/",      # the pipeline's own definitions
    "agents/",
    "guardrails/",
    "pipeline.yaml",
)


def protected_paths(diff: str) -> list[str]:
    """Files a patch touches that it must not."""
    touched = re.findall(r"^\+\+\+ b/(.+)$", diff, flags=re.MULTILINE)
    return sorted({path for path in touched if path.startswith(PROTECTED)})
```

A fix that edits CI configuration is not a fix. Neither is one that edits the guardrails constraining
the agent that wrote it. The guardrail text tells the agent not to — but the guardrail is a request,
and this is the enforcement.

Note the asymmetry in the tests: the refusals are tested first and in more detail than the successes.
For a trust boundary, that's the right ratio.

```python
def test_applying_a_patch_that_escapes_the_source_tree_is_refused(tmp_path):
    """The agent has no write permission; this is the only thing that writes, so it decides."""
    result = CliRunner().invoke(apply_patch, [f"--patch={escape_patch}", f"--repo={repo}"])
    assert result.exit_code == 1
    assert "protected paths" in result.output
```

### Builtin 3 — `run-suite`, a verdict the pipeline can branch on

```python
@click.option("--expect", type=click.Choice(["pass", "fail"]), default="pass")
def run_suite(repo, suite, select, output, expect):
    """Run the target project's own tests and turn the result into a verdict.

    `--expect fail` is what makes a reproducer meaningful: a test that does not fail before the fix
    proves nothing about the bug, so the pipeline asserts the failure first and the pass afterwards.
    """
```

`--expect fail` earns its place. The pipeline runs each reproducer **before** any fix exists and
requires it to fail. A reproducer that passes against the unfixed code isn't reproducing anything,
and without this check the pipeline would happily "fix" bugs whose tests never demonstrated them.

The command also translates between test runners, so the pipeline doesn't need to know whether the
target uses pytest, jest, go test or cargo — it asks for a verdict and gets one.

### Testing your extension

Extensions get tested like any other package. The framework's own suite runs them:

```console
$ cd examples/bug-fix/extensions
$ uv run --with-editable . python -m pytest tests -q
12 passed
```

---

## Part 2 — Telling the compiler your builtins exist

Here is the constraint that shapes this: **the compiler never imports `pipeline-exec`.** A generated
repository installs the runtime, not the compiler — a runtime dependency in the compiler would invert
that. So `lockstep` cannot discover your commands by introspection, and a `builtin:` step naming one
would be rejected:

```console
LS200: commands/fix-bugs.md step 1 — builtin 'jira-fetch' is not provided by pipeline-exec
  hint: available: check-convergence, collect-failures, discover, list-commands, report,
        test-runner, validate-schema, wait-for. If an extension provides it, list it under
        `extensions.builtins` in pipeline.yaml — the compiler cannot discover a command it
        does not install
```

Declare them in the manifest:

```yaml
# pipeline.yaml
extensions:
  builtins: [jira-fetch, apply-patch, run-suite]
  packages: ["bugfix-ext @ file://./extensions"]
```

`builtins` is what the compiler will accept. `packages` is what a generated repository must install
to actually get them — and `doctor` will refuse the first without the second:

```console
error: DOC013: 3 extension builtin(s) declared but no package provides them
       list the distributions under `extensions.packages` so a generated repository installs
       them; otherwise the workflow will fail with `No such command`
```

With both present, `doctor` still tells you what it cannot check:

```console
warning: DOC014: extension builtins are not verifiable here: apply-patch, jira-fetch, run-suite
         run `pipeline-exec list-commands` in CI with the extension installed to prove they
         exist before a scheduled run finds out they do not
```

That warning is the honest position: a declaration is a promise, and the compiler is telling you it
took your word for it. Close the loop in CI by installing the extension and running
`pipeline-exec list-commands --extensions-only` — which is exactly what the pipeline's own overlay
does, in the job before the first builtin step runs.

---

## Part 3 — Extending with a composite action

### When an action is the right answer

Reach for a composite action when your work must **compose other actions**. That's essentially the
only reason: `actions/checkout`, `actions/setup-*`, `actions/cache` and their kin can only be invoked
from a workflow or another action, never from a script.

Our case qualifies. The application being fixed lives in a different repository, and it needs a
toolchain installed:

```yaml
# examples/bug-fix/extensions/actions/setup-target/action.yml
name: Set up the target repository
description: >-
  Check out the application being fixed and install its toolchain.

  This is a composite action rather than a script step because it needs other actions —
  `actions/checkout` for a second repository and a language setup action — and a script cannot call
  those.

inputs:
  repository: { description: The application repository, as owner/name., required: true }
  path:       { description: Where to place it., required: false, default: target }
  token:      { description: Token with read access., required: false, default: "" }

outputs:
  sha:
    description: The commit that was checked out, so a fix can name what it was written against.
    value: ${{ steps.record.outputs.sha }}

runs:
  using: composite
  steps:
    - uses: actions/checkout@v4
      with:
        repository: ${{ inputs.repository }}
        path: ${{ inputs.path }}
        token: ${{ inputs.token || github.token }}
        fetch-depth: 0        # the analysing agent reads history to find when a bug arrived
    - uses: actions/setup-python@v5
      with: { python-version: ${{ inputs.python-version }} }
    - id: record
      shell: bash
      working-directory: ${{ inputs.path }}
      run: echo "sha=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"
```

`fetch-depth: 0` earns its cost here: the analysing agent uses `git log -S` to find when a line
arrived, which shallow history can't answer.

### Getting the compiler to emit it

The compiler only emits actions it knows about. Yours arrives through an **overlay** — the sanctioned
mechanism for GitHub-specific things the spec deliberately doesn't model:

```yaml
# overlays/github/setup-target.yml
target: workflows/fix-bugs.yml
patches:
  - op: insert-step
    at: jobs[id=fetch-bugs].steps
    after: fetch-bugs
    value:
      name: Set up the application being fixed
      id: setup-target
      uses: ./extensions/actions/setup-target
      with:
        repository: ${{ vars.TARGET_REPO }}
        token: ${{ secrets.TARGET_REPO_TOKEN }}
        path: outputs/target

  # The extension's commands have to exist before a builtin step names one.
  - op: insert-step
    at: jobs[id=fetch-bugs].steps
    before: fetch-bugs
    value:
      name: Install the pipeline's extensions
      id: install-extensions
      run: |
        set -euo pipefail
        python -m pip install --quiet ./extensions
        pipeline-exec list-commands --extensions-only
```

Two things to notice.

**Anchors key on step `id:`, not on labels.** This is why the command gives its steps explicit ids.
Rename a step's display label later and these patches still apply; without ids they would attach to a
generated slug and detach silently the first time somebody rewords a heading.

**An anchor that matches nothing is a hard error**, with the nearest candidate named. I got this
wrong while writing the example:

```console
OVL404: overlays/github/setup-target.yml hunk 1 — no `fetch-triaged-bugs` in generated output
      hint: nearest: fetch-bugs
```

That's the designed behaviour: a patch that silently doesn't apply is worse than a build that fails,
because a security-relevant customization that quietly stopped applying is invisible.

Once the checkout lands in the first job, `outputs/target` travels to every later job through the
workspace artifact that `restore`/`save` already carry. You don't need the action again.

---

## Part 4 — The pipeline itself

With both extensions in place, the spec reads as ordinary steps:

```markdown
1. **Fetch triaged bugs** → builtin: jira-fetch
   - id: fetch-bugs
   - args: --jql="{jql}" --limit={limit} --output={output_dir}/bugs.json

2. **Analyze each bug against the source** → agent: bug-analyst
   - foreach: bug in {output_dir}/bugs.json
   - output: {output_dir}/analyses
   - parallel: 3
   - min-success-rate: 0.8

3. **Write a reproducer for each analyzed bug** → agent: reproducer-writer
   - foreach: analysis in {output_dir}/analyses.json
   - output: {output_dir}/reproducers
   - parallel: 3

4. **Prove each reproducer fails before the fix** → builtin: run-suite
   - id: prove-reproducers
   - args: --repo={output_dir}/target --suite=pytest --expect=fail --output={output_dir}/reproduced.json

5. **Write a fix for each reproduced bug** → agent: fix-writer
   - foreach: bug in {output_dir}/reproduced.json
   - output: {output_dir}/patches
   - parallel: 2
   - min-success-rate: 0.5

6. **Apply the patches** → builtin: apply-patch
   - id: apply
   - args: --patch={output_dir}/patches/combined.patch --repo={output_dir}/target

7. **Prove the suite passes after the fix** → builtin: run-suite
   - id: validate-fixes
   - args: --repo={output_dir}/target --suite=pytest --expect=pass --output={output_dir}/validated.json

8. **Review the fixes** → agent: fix-reviewer
   - input: {output_dir}/validated.json
   - output: {output_dir}/review.json
   - context-files: {output_dir}/patches/combined.patch

9. **Assemble what passed review** → script: scripts/assemble-fixes.py
   - args: --review={output_dir}/review.json --patches={output_dir}/patches --output={output_dir}/fixes
   (if not --dry-run)
```

Note the `min-success-rate` values, which encode a real judgement:

- **0.8 for analysis** — most bugs should be locatable; if a fifth aren't, something is wrong with
  the input rather than with any one bug.
- **0.5 for fix-writing** — half the bugs producing a viable patch is a *good* run. Writing code is
  the hardest step and failing at it is normal. Setting this to 1.0 would fail the whole pipeline
  because one hard bug resisted, throwing away the fixes that worked.

### The agents

Four agents, each with a narrow job, and every one of them read-only:

| Agent | Turns | Tools | Why |
|---|---|---|---|
| `bug-analyst` | 12 | filesystem, git (read) | Must explore; the codebase can't fit in a prompt |
| `reproducer-writer` | 0 | none | Has the analysis; needs no exploration |
| `fix-writer` | 8 | filesystem (read) | Needs surrounding code, not the whole repo |
| `fix-reviewer` | 0 | none | Given the diff and the results; more input would dilute the review |

Zero turns where zero will do. An agent with no tools cannot wander, costs one round trip, and is
trivially reproducible.

The reviewer is a separate agent from the writer on purpose, and prompted adversarially:

> Assume they are wrong until the evidence says otherwise. […] A passing test suite is evidence, not
> proof — the suite only covers what somebody already thought to test.

An author reviewing their own work is a formality. The point of the step is a second opinion that
costs nothing to obtain and can decline.

### Guardrails that are enforced, not requested

```yaml
---
name: common
enforce:
  permissions: read-all
  deny-tools: [write_file, delete_*, create_*, update_*]
---
```

The `enforce` block compiles into permissions and tool allow-lists the model **cannot exceed**. Even
though `mcp/servers.json` lists only read tools for the filesystem and git servers, `deny-tools`
holds the line independently — so a later edit that adds `write_file` to that server does not
silently hand every agent write access.

The prose guardrails carry the judgement the substrate can't:

```
A fix MUST change as little as possible to make the reproducer pass.

You MUST NOT reformat code you are not fixing, rename anything for clarity, upgrade a dependency,
or "tidy" adjacent code. Every unrelated line in a diff is a line a reviewer has to read and a
risk nobody asked for.

You MUST NOT modify tests to make them pass. If the reproducer is wrong, say so — do not weaken it.
```

That last line matters. The most likely way an agent "fixes" a bug is by weakening the test that
detects it. Saying so in the guardrail is the cheap defence; `apply-patch` refusing to touch
protected paths is the one that actually holds.

---

## Part 5 — Verifying the whole thing

```console
$ lockstep lint --root examples/bug-fix
lint:
  no findings
0 error(s), 0 warning(s)

$ lockstep doctor --root examples/bug-fix
doctor:
  warning: DOC014: extension builtins are not verifiable here: apply-patch, jira-fetch, run-suite
0 error(s), 1 warning(s)

$ lockstep compile --root examples/bug-fix
fix-bugs: 9 steps -> 11 jobs · 4 agentic, 5 deterministic, 4 cacheable
wrote 19 files
```

The compiled graph, and where the trust sits:

| Job | Kind | Permissions |
|---|---|---|
| `fetch-bugs` | steps | contents: read, actions: read |
| `analyze-each-bug-against-the-source` | agent ×3 | *read-all* |
| `verify-analyze-…` | steps | read |
| `write-a-reproducer-for-each-analyzed-bug` | agent ×3 | *read-all* |
| `prove-reproducers` | steps | contents: read, actions: read |
| `write-a-fix-for-each-reproduced-bug` | agent ×2 | *read-all* |
| `verify-write-a-fix-…` | steps | read |
| `apply` | steps | contents: read, actions: read |
| `review-the-fixes` | agent | *read-all* |
| `assemble-what-passed-review` | steps | contents: read, actions: read |
| `propose-generated-artifacts` | steps | **contents: write, pull-requests: write** |

**One write job, at the very end, holding no model.** Four agents read your source and propose
changes to it; none of them can touch anything. The patch they produce travels through an artifact,
past a code-enforced protected-path check, through a test suite that must fail then pass, past an
adversarial reviewer, into a deterministic assembly step — and only then into a branch that a human
merges.

That chain is the answer to "should an AI write code in my repository". Each link is cheap; together
they mean the worst outcome of a bad run is a pull request somebody declines.

---

## Part 6 — Checklist for your own extension

**A builtin, when the work is code:**

1. Create a package with a `pipeline_exec.commands` entry point per command.
2. Write ordinary Click commands. Publish outputs to `$GITHUB_OUTPUT` when it exists.
3. Test it — especially the refusals, if it's a trust boundary.
4. Declare it in `pipeline.yaml` under `extensions.builtins`, and the distribution under
   `extensions.packages`.
5. Install it in CI and run `pipeline-exec list-commands --extensions-only` to close the loop
   `doctor` can't.

**A composite action, when the work must compose other actions:**

1. Write `action.yml` with explicit `inputs:` and `outputs:`.
2. Insert it with an overlay, anchored on a step `id:` — and give that step an explicit `id:` in the
   spec so the anchor survives a rename.
3. Publish it wherever `capabilities.actions` points, or reference it by path as the example does.

**Neither, when a script will do.** Most of what a pipeline needs is a script step. Extensions are
for the two cases above, and reaching for one when a script would do is how a pipeline accumulates
machinery nobody wants to maintain.

---

## Honest limits

- The extension mechanism is exercised for real: the example's commands are loaded through entry
  points, their tests pass, and the compiled workflow invokes them. But like everything else here,
  **none of it has run on a real GitHub runner**.
- `jira-fetch` is written against Jira's v2 search API and paginates, but has only been tested
  against its own unit tests — not a live instance.
- The bug-fix pipeline is a worked example of the extension points, not a validated approach to
  automated code repair. The parts that make it defensible — read-only agents, a code-enforced patch
  boundary, fail-then-pass validation, adversarial review, human merge — are the parts worth copying
  regardless of what your pipeline does.
