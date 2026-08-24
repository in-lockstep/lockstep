# Reviewing pull requests on request

This guide builds a pipeline that reviews a pull request when somebody asks it to, through whichever
lenses they name:

```
/review security
/review intent
/review security intent tests
/review
```

Each aspect produces **its own review** — a separate, dismissable opinion in the pull request's review
list, the way a human reviewer would have left them. And asking again after nothing has changed does
nothing at all, because a second review saying the same thing is worse than no review.

The finished example is [`examples/pr-review`](../examples/pr-review). It builds on
[the chat-ops guide](implementing-issues.md), which covers the command gate in detail.

---

## The shape

```
gate ──▶ fetch diff ──▶ what still needs reviewing ──┬──▶ security review ──┐
        (builtin)              (builtin)             ├──▶ intent review ────┤
                                    │                ├──▶ performance ──────┼──▶ post
                                    │                └──▶ test coverage ────┘   (script)
                        nothing changed? nothing is
                        due, and no agent ever starts
```

**One agent per lens**, each gated on whether this run needs it. Everything else is deterministic —
including, importantly, the decision about *whether to review at all*.

---

## Part 1 — A lens is an agent

`/review security intent` asks for two reviews. `/review security` asks for one. `/review` asks for
all of them. How many is not known until somebody types it, so these cannot be declared as named
arguments:

```yaml
github:
  command:
    name: "/review"
    events: [issue_comment, pull_request_review_comment]
    roles: [admin, maintain, write]
    associations: [OWNER, MEMBER, COLLABORATOR]
```

The bare words after the command arrive as a JSON array:

```
$ pipeline-exec parse-command --command=/review --body='/review security intent'
matched=true
positional=["security", "intent"]
```

Each word names an agent:

```
agents/
├── security-reviewer.md
├── intent-reviewer.md
├── performance-reviewer.md
└── tests-reviewer.md
```

```markdown
---
name: security-reviewer
description: Review a pull request for ways in
model: claude-sonnet-4-6
max_tool_turns: 6
guardrails: [common, reviewing]
skills: [review-writing, review-revision]
github:
  max-ai-credits: 90
---

You review one pull request for security, and for nothing else.

Look for the ways this diff could be exploited, not for the absence of best practices.
…
## What this codebase has already decided

All database access goes through `src/repo.py`. A query assembled anywhere else is worth flagging
even when it looks parameterised, because it is outside the layer that was audited.
```

### Why an agent rather than a data file

An earlier version of this pipeline kept the lenses as prose files in an `aspects/` directory and
passed the text through as JSON data to one shared agent. It reads like less machinery, and it costs
three things that turn out to matter.

**Evals.** `lockstep lint` requires eval cases per agent, so four lenses means four suites, and
"does the security lens find the planted traversal" is a gate rather than a hope:

```
evals/security-reviewer/cases/
├── path-traversal.json                 finds it, cites the line, says what an attacker does
├── format-from-the-query.json          follows the new parameter into a file the diff never touched
├── query-outside-the-repo-layer.json   flags a parameterised query outside src/repo.py
├── nothing-to-find.json                a changelog typo: reports nothing, in one sentence
└── revising-a-fixed-finding.json       says "fixed" rather than repeating or silently dropping
```

With one shared agent this cannot be expressed — and the eval that existed had a hand-copied
one-sentence excerpt of the lens pasted into its input, which had already drifted from the real file.
Now the case supplies the diff and, where the answer depends on code the diff did not touch, a
fixture tree beside it; the lens comes from the agent under test, so there is nothing to drift.

The second case is the one worth reading. It carries `fixtures/format-from-the-query/`, and its
deterministic half asserts that the review names `src/reports/export.py` — a file that appears in no
patch. A reviewer working from the diff alone cannot pass that line by luck. Its rubric is scored
rather than decided, so the difference between *traces the parameter into the file read* and *says
the parameter is unvalidated* is a 5 and a 3 rather than two passes. See [evals](evals.md).

**Budget and model per lens.** A security review is worth more than a test-coverage review, and can
now cost more:

```python
def test_a_lens_may_cost_what_it_is_worth():
    credits = {name: agent.github.max_ai_credits for name, agent in spec.agents.items()}
    assert credits["security-reviewer"] > credits["tests-reviewer"]
```

**Somewhere to put what this codebase decided.** "All DB access goes through `src/repo.py`" is the
most valuable sentence in a security review and the least portable. It reaches exactly the reviewer
that needs it — a context would inject it into all four. See [what goes where](layers.md).

What it costs: adding a lens is adding an agent, a step, and its eval cases, rather than dropping in
a file. That is the right friction — an untested lens reports plausible nonsense confidently.

The shared parts do not get copied four times. The revision protocol is method, so it is a skill:

```yaml
skills: [review-writing, review-revision]
```

---

## Part 2 — Deciding which reviews are due

One builtin resolves the request and the state of the pull request together, and publishes what it
decided:

```markdown
2. **Work out which reviews are still due** → builtin: review-state
   - id: state
   - emits: pending
   - args: --pr="{pull_request}" --requested="{positional}" --available=security,intent,performance,tests --output-dir={output_dir}/pending
```

`emits: pending` lifts a value the step computed into a job output, which is what the reviewing steps
gate on:

```markdown
3. **Review for security** → agent: security-reviewer
   (if security in {state.pending})
   - input: {output_dir}/pending/security.json
   - output: {output_dir}/reviews/security.json
   - context-files: {output_dir}/diff.json
```

compiling to:

```yaml
review-for-security:
  needs: [command-gate, diff]
  if: ${{ !failure() && !cancelled()
      && needs.command-gate.outputs.authorized == 'true'
      && needs.diff.outputs.pending != ''
      && contains(fromJSON(needs.diff.outputs.pending), 'security') }}
  uses: ./.github/workflows/aw-security-reviewer.lock.yml
```

Three things in that condition are load-bearing. `!failure() && !cancelled()` keeps the posting step
alive when a lens was skipped. The authorization check is asserted on every job, never inherited. And
`!= ''` guards the `fromJSON` — a skipped upstream job publishes an empty string, and `fromJSON('')`
is an error rather than false.

The four reviewing jobs all depend on `diff` and none on each other, so two requested lenses run at
once rather than in a queue:

```python
def test_the_reviews_run_beside_each_other_not_in_a_queue(workflow):
    for job in REVIEW_JOBS.values():
        assert workflow["jobs"][job]["needs"] == [GATE, "diff"]
    assert workflow["jobs"]["post"]["needs"] == [GATE, *REVIEW_JOBS.values()]
```

### Validating the request in code, not in a prompt

```python
unknown = [word for word in words if word not in available]
if unknown:
    _fail(f"unknown review aspect(s): {', '.join(unknown)}. available: {', '.join(sorted(available))}")
```

`/review banana` fails with a message naming what *is* available. It does not reach a model, because
a model asked for a banana review will produce one and it will look plausible. One unknown aspect
refuses the whole request rather than silently reviewing two of three — partial success that looks
like success is the worst outcome available.

`/review` with no words reviews everything, and `/review security security` reviews once.

---

## Part 3 — Not reviewing what has not changed

This is the part that decides whether people keep the bot switched on.

Somebody comments `/review security`. They read it, push a fix, and comment `/review security`
again — that second run should review the new code. But if they comment `/review security` twice
without pushing anything, the second should do **nothing**. And a scheduled or accidental re-run
should not bury their conversation under a duplicate.

### The record lives in the review itself

There is nowhere durable to keep "what did I review, and when" except the artifact itself. So each
review carries a marker:

```
<!-- lockstep:review aspect=security sha=abc12345 -->
## Security review
…
```

and `review-state` reads it back:

```python
def previous_reviews(reviews):
    """The bot's own most recent review per aspect, found by its marker.

    Latest wins: a review revised several times leaves several entries, and only the last one
    describes the current state.
    """
```

### The decision

```python
def plan(aspects, reviews, head, commits, *, force=False):
    """Split the requested aspects into work to do and work already done."""
    for aspect in aspects:
        seen = previous.get(aspect["key"])
        if seen and seen["sha"] == head and not force:
            skipped.append({"key": key, "reason": f"already reviewed at {head[:8]}; …"})
            continue
        …
```

Per aspect, not per run — which is what you want. `/review security intent` where security was
already reviewed at this commit but intent was never reviewed runs exactly one review.

```python
def test_an_unchanged_pull_request_is_not_reviewed_again():
    """A second review saying the same thing buries the human conversation."""
    pending, skipped = plan(ASPECTS, [review("security", "ccc33333")], "ccc33333", COMMITS)
    assert [item["key"] for item in pending] == ["intent"]
```

The nicest part of this is what it costs to implement: **nothing**. The step publishes a shorter
`pending` list, and every lens not in it fails its own condition — so those jobs never start and the
run costs nothing. An empty list starts no reviewers at all.

```python
def test_nothing_reviews_a_pull_request_that_has_not_moved(workflow):
    outcome = simulate(workflow, {}, {**AUTHORIZED, "diff": {"pending": "[]"}})
    assert not [job for job in outcome.order if job.startswith("review-for-")]
```

`--force` exists for when you want it anyway.

### A force-push is the interesting case

If somebody rewrites history, the commit a review was made against may no longer exist:

```python
def test_a_force_push_that_erased_the_reviewed_commit_reviews_everything():
    """Pretending nothing changed because the history was rewritten is the wrong answer."""
    assert len(commits_since(COMMITS, "deadbeef")) == len(COMMITS)
```

When the reviewed commit is not in the history, everything is treated as new. Reviewing too much is
recoverable; silently reviewing nothing is not.

---

## Part 4 — Revising rather than repeating

When the pull request *has* moved, the second review must not appear beside the first. A reviewer who
addressed a finding wants to see it resolved, not restated below itself.

### The agent knows it is revising

Each pending aspect carries what was said before and what has happened since:

The state step writes one file per pending lens, and each reviewing step reads its own:

```json
{
  "key": "security",
  "revision": true,
  "previous_review_id": 42,
  "previous_review": "<!-- lockstep:review … -->\n## Security review\n…",
  "previously_reviewed_sha": "aaa11111",
  "new_commits": [
    { "sha": "bbb22222", "message": "Validate the path" },
    { "sha": "ccc33333", "message": "Add a test" }
  ]
}
```

and every reviewer is told what to do with it. Not four times: the revision protocol is *method*, so
it is one skill that all four declare, and `shared/skill-review-revision.md` is what actually reaches
each prompt.

```markdown
You may be given your previous review and the commits pushed since. You are revising that review, not
writing a new one — it will replace what you said last time.

Go through your earlier findings one at a time and decide, for each: fixed, still standing, or no
longer relevant because the code moved. Say which. A finding that silently disappears looks like you
changed your mind; a finding repeated after somebody fixed it looks like you did not read their work.

Then read the new commits for what they changed about your earlier conclusion — including anything
they introduced that was not there to find before.

If the new commits resolved everything and raised nothing, say that. It is the most useful review you
can leave, and the one that makes the next one worth reading.
```

```python
@pytest.mark.parametrize("aspect", ASPECTS)
def test_every_reviewer_is_told_how_to_revise(aspect):
    front = yaml.safe_load(files[f".github/workflows/aw-{aspect}-reviewer.md"].split("---")[1])
    assert "shared/skill-review-revision.md" in front["imports"]
```

The guardrail makes the two failure modes non-negotiable:

```markdown
You MUST NOT repeat a finding the author has already addressed. Check what changed before restating
what you said last time.

You MUST NOT drop a previous finding without saying so. Either it was fixed, it still stands, or the
code moved past it — a reader who cannot tell which will not trust the next review either.
```

### The update itself

Which review to revise comes from the pipeline's own record rather than from the agent echoing it
back — an agent that forgot would post beside its earlier review instead of replacing it:

```bash
previous=""
[ -f "${pending}/${aspect}.json" ] && previous=$(jq -r '.previous_review_id // empty' "${pending}/${aspect}.json")

if [ -n "$previous" ]; then
  # A submitted review's body can be updated; its inline comments cannot. The revision therefore
  # goes into the body, and the thread keeps one review per aspect however often the branch moves.
  jq -n --arg body "$body" '{body: $body}' \
    | gh api -X PUT "repos/${GITHUB_REPOSITORY}/pulls/${pr}/reviews/${previous}" --input -
```

That constraint is worth stating plainly, because it shapes the design: **GitHub lets you edit a
submitted review's body, not its inline comments.** So the first review of an aspect posts inline
comments on the diff where they are actionable, and revisions update the body — which lists every
current finding with its location anyway. One review per aspect, however many times the branch moves.

---

## Part 5 — One review per aspect

```bash
for file in "$reviews"/*.json; do
  aspect=$(basename "$file" .json)
  …
  gh api -X POST "repos/${GITHUB_REPOSITORY}/pulls/${pr}/reviews" --input -
done
```

Each reviewer wrote `outputs/reviews/<aspect>.json`; the posting step turns each into its own
review. It is one job posting N reviews rather than N jobs posting one each, for two reasons: an
agent job is a `uses:` call with no room for extra steps, and a partial failure should lose one
aspect rather than the run.

```bash
else
  # One aspect failing to post must not lose the others.
  echo "::warning::could not post the $aspect review"
fi
```

The script is bash because it needs `gh` and the job's token, but it is exercised for real — against
a stubbed `gh` that captures what would have been sent:

```python
def test_each_aspect_becomes_its_own_review(workspace):
    """`/review security intent` produces two reviews, not one mentioning both."""
    assert calls.count("pulls/7/reviews") == 2


def test_a_previously_reviewed_aspect_is_revised_in_place(workspace):
    """A reviewer who addressed a finding wants it resolved, not repeated below itself."""
    assert "-X PUT" in calls
    assert "pulls/7/reviews/42" in calls
```

### A review with nothing to say is still posted

```python
def test_a_review_with_nothing_to_say_is_still_posted(workspace):
    """Silence is ambiguous; "nothing found" is a result somebody can act on."""
```

If you asked for a security review and got nothing back, you cannot tell whether it found nothing or
failed. The guardrail encourages the same thing from the other direction:

```markdown
You MUST NOT manufacture a finding to appear useful. "Nothing to report for this aspect" is a
complete and valuable review, and a bot that always finds something is a bot people mute.
```

---

## Part 6 — What the reviewer actually sees

Reviewing is a lens, and most of the quality comes from the lens being narrow. The agent's own
instruction is almost entirely about staying in it:

```markdown
You are given one review aspect and one pull request. Review the pull request through that aspect
and nothing else.

The aspect you were given carries its own brief. Follow it. If you notice something real that belongs
to a different aspect, leave it — another review is looking at that, or nobody asked for it. A review
that wanders is a review the reader cannot skim.
```

The `reviewing` guardrail carries what any review must not do, regardless of aspect:

```markdown
Your review is posted publicly on somebody's pull request, under their name in their repository.

You MUST NOT report a finding you cannot point at. Every finding names a file, and a line where one
exists. A concern that cannot be located cannot be acted on or dismissed.

You MUST NOT report the absence of something this codebase does not do anywhere. That is a proposal
about the project, not a review of this change.

You MUST NOT comment on style, formatting, or naming unless it changes behaviour. A linter that the
project chose not to run is not a finding.

You MUST NOT judge the author. Review the change.
```

And the context says what this repository already checks, so the bot does not report what CI told the
author first:

```markdown
The repository is a Python service. It runs `ruff`, `mypy --strict` and `pytest` on every pull
request, so anything those tools would catch is already caught — a review that reports it is telling
the author something CI told them first.
```

### What the diff step withholds

```python
GENERATED = ("package-lock.json", "yarn.lock", "uv.lock", "Cargo.lock", "go.sum", ".lock.yml")
MAX_PATCH = 24_000
MAX_TOTAL = 180_000
```

Lock files never carry a review finding. Enormous patches are truncated. And a total budget stops one
huge pull request from crowding everything else out. Crucially, whatever is withheld is **named**:

```python
def test_what_was_skipped_is_named_rather_than_dropped():
    """A reviewer must know what it did not see."""
    assert any("uv.lock" in entry for entry in diff["not_reviewed"])
```

Truncating deliberately and saying so beats truncating implicitly by running out of context, where
the review looks complete and silently is not.

---

## Part 7 — Permissions

| Job | Permissions |
|---|---|
| `command-gate` | contents: read |
| `select` (aspects, diff, state — fused) | contents: read, actions: read |
| `review-one-aspect` ×N | *the agent workflow declares read-all* |
| `verify-review-one-aspect` | inherited read-only |
| `post` | **contents: read, pull-requests: write** |

One write, granted explicitly by an overlay so every write this pipeline performs is visible in one
file:

```yaml
- op: merge
  at: jobs[id=post]
  value:
    # The only write in the pipeline. It runs no model: the agents that wrote these reviews cannot
    # reach the pull request they are about.
    permissions:
      contents: read
      pull-requests: write
```

Every reviewer has `permissions: read-all`, no MCP servers, and only the default network. It reads
the diff it was handed and its own pending file, and writes JSON to one path. It cannot reach the
pull request it is reviewing, cannot post anything, and cannot see the repository beyond what the
diff step chose to give it.

They carry `max_tool_turns: 6` rather than `0`. An earlier version said `0`, which was wrong in a way
worth naming: a reviewer is handed its diff as a *path*, and an agent with no turns can neither open
that file nor write its result. Zero reads as maximally locked down and is really just broken.

---

## Part 8 — Running it

```bash
lockstep lint --root examples/pr-review      # no findings
lockstep doctor --root examples/pr-review    # extensions unverifiable here; otherwise clean
lockstep compile --root examples/pr-review
```

In the repository:

```bash
gh api -X PUT repos/:owner/:repo/environments/repo
gh aw compile
```

Then, on any pull request:

```
/review security intent
```

Two reviews appear, from two jobs named `review-for-security` and `review-for-intent` — which is
worth more than it sounds when somebody is watching the checks list to see whether the bot is
working. Push a fix and ask again: the security review updates in place, saying what your commits
resolved. Ask again without pushing and nothing happens at all.

---

## Honest limits

- Nothing here has run on a real GitHub runner. The marker round-trip, the review update, and the
  inline-comment anchoring are contract-tested and exercised against a stubbed `gh`, but not against
  the real API.
- Review markers live in the review body, which means a maintainer who edits the bot's review can
  break the state tracking. Recovering is harmless — the aspect is treated as never reviewed and
  posted fresh — but it does mean a duplicate.
- Inline comments anchor to lines in the diff. A finding on a line that later moves will follow
  GitHub's usual outdated-comment behaviour; the pipeline does not try to re-anchor it.
- `pr-diff` budgets are fixed constants. A repository with genuinely enormous reviewable diffs will
  want them configurable.
- The pipeline reviews the diff, not the repository. A reviewer cannot look at a file the diff did
  not touch, which is a real limit on what a security review can conclude — "is this the only caller"
  is unanswerable from a diff. Giving the security reviewer alone a read-only filesystem server would
  change that, at the cost of latency and a wider surface. That it can be given to *one* lens rather
  than all four is the clearest argument for a lens being an agent.
- The four lenses shipped here are illustrative. Their "what this codebase has already decided"
  sections describe an example service, and are the first thing to replace when adopting this.
