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
gate ──▶ select aspects ──▶ fetch diff ──▶ what still needs reviewing ──▶ review ×N ──▶ post
        (script)            (builtin)            (builtin)              (agent, ×4)   (script)
                                                      │
                                          nothing changed? empty list,
                                          and no agent ever starts
```

One agent, run once per requested aspect. Everything else is deterministic — including, importantly,
the decision about *whether to review at all*.

---

## Part 1 — Fanning out over what somebody typed

`/review security intent` asks for two reviews. `/review security` asks for one. `/review` asks for
all of them. How many is not known until somebody types it, so these cannot be declared as named
arguments:

```yaml
github:
  command:
    name: "/review"
    events: [issue_comment, pull_request_review_comment]
    roles: [admin, maintain, write, read]
```

Note `read` in the roles. Asking a bot to look at a pull request is not a privileged action — unlike
the pipelines that *write* code, this one only reads and comments, so a contributor without merge
rights can still ask for a security review of their own work.

The bare words after the command arrive as a JSON array:

```
$ pipeline-exec parse-command --command=/review --body='/review security intent'
matched=true
positional=["security", "intent"]
```

and reach the pipeline as `{positional}`:

```markdown
1. **Work out what was asked for** → script: scripts/select-aspects.py
   - id: select
   - args: --requested="{positional}" --aspects-dir=aspects --output={output_dir}/aspects.json
```

which compiles to `--requested="${{ needs.command-gate.outputs.positional }}"`.

### An aspect is a file

```
aspects/
├── security.md
├── intent.md
├── tests.md
└── performance.md
```

Each is a markdown file with frontmatter and a brief:

```markdown
---
name: security
title: Security
summary: Whether this change introduces a way in
---

Look for the ways this diff could be exploited, not for the absence of best practices.

Concretely: input that reaches a query, a filesystem path, a shell, or a template without being
constrained. Authorization checks that a new code path bypasses. Secrets that reach a log, an error
message, or a response body.

Say what an attacker would do, in order. "This is unsanitized" is not a finding; "a `name` of
`../../etc/passwd` reaches `open()` on line 84" is.

Do not report the absence of a control that this codebase does not use anywhere. That is a design
discussion, not a review of this change.
```

**Adding a review lens is adding a file.** No pipeline change, no code change, no new agent:

```python
def test_adding_an_aspect_is_adding_a_file(tmp_path):
    (tmp_path / "clarity.md").write_text(
        "---\nname: clarity\ntitle: Clarity\nsummary: Whether it reads well\n---\n\nLook for…\n"
    )
    loaded = select_aspects.load_aspects(tmp_path)
    assert loaded["clarity"]["title"] == "Clarity"
```

### Validating the request in code, not in a prompt

```python
def select(requested, aspects):
    if not requested:
        return [aspects[name] for name in sorted(aspects)]
    unknown = [name for name in requested if name not in aspects]
    if unknown:
        raise KeyError(f"unknown review aspect(s): {', '.join(unknown)}. available: …")
```

`/review banana` fails with a message naming what *is* available. It does not reach a model, because:

```python
def test_an_unknown_aspect_is_refused_and_says_what_is_available():
    """A model asked for a "banana review" will produce one, and it will look plausible."""
```

And one unknown aspect refuses the whole request rather than silently reviewing two of three —
partial success that looks like success is the worst outcome available.

`/review` with no words reviews everything, `/review security security` reviews once, and the order
you asked in is the order you get.

---

## Part 2 — Not reviewing what has not changed

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

The nicest part of this is what it costs to implement: **nothing**. The state step writes a shorter
work list, `fanout` produces fewer matrix legs, and if the list is empty there are no legs at all —
so the agent never starts and the run costs nothing. The existing machinery does the work.

```python
def test_nothing_to_review_writes_an_empty_list(tmp_path, fixtures):
    """An empty work list means an empty matrix, and the agent never starts."""
    assert json.loads(output.read_text()) == []
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

## Part 3 — Revising rather than repeating

When the pull request *has* moved, the second review must not appear beside the first. A reviewer who
addressed a finding wants to see it resolved, not restated below itself.

### The agent knows it is revising

Each pending aspect carries what was said before and what has happened since:

```json
{
  "key": "security",
  "brief": "…",
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

and the agent is told what to do with it:

```markdown
## When you have reviewed this before

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

The guardrail makes the two failure modes non-negotiable:

```markdown
You MUST NOT repeat a finding the author has already addressed. Check what changed before restating
what you said last time.

You MUST NOT drop a previous finding without saying so. Either it was fixed, it still stands, or the
code moved past it — a reader who cannot tell which will not trust the next review either.
```

### The update itself

```bash
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

## Part 4 — One review per aspect

```bash
for file in "$reviews"/*.json; do
  aspect=$(basename "$file" .json)
  …
  gh api -X POST "repos/${GITHUB_REPOSITORY}/pulls/${pr}/reviews" --input -
done
```

Each matrix leg wrote `outputs/reviews/<aspect>.json`; the posting step turns each into its own
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

## Part 5 — What the reviewer actually sees

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

## Part 6 — Permissions

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

The reviewing agent has `max_tool_turns: 0`, no MCP servers, and `permissions: read-all`. It receives
a diff and returns JSON. It cannot fetch anything, cannot write anything, and cannot see the
repository outside the diff it was handed.

---

## Part 7 — Running it

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

Two reviews appear. Push a fix and ask again — the security review updates in place, saying what your
commits resolved; the intent review says nothing changed for it, or updates too if it did. Ask again
without pushing and nothing happens at all.

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
- The pipeline reviews the diff, not the repository. An agent with `max_tool_turns: 0` cannot look at
  a file the diff did not touch, which is a real limit on what a security review can conclude. Giving
  it a read-only filesystem server would change that, at the cost of latency and a wider surface.
