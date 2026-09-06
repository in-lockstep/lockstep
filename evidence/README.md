# evidence

Cases promoted out of real runs, kept on purpose.

Every review this repository pays for is recorded, and `eval harvest` turns the recording into a
case: a request that was really sent, with expectations derived from the answer that really came
back (`GATE-RECORD-1`). Those arrive as candidates, in the run artifact, and expire with it — 30
days, and nobody has to act for that to happen. This directory is the other end: the few a person
decided were worth keeping.

`make check` settles everything here on every commit, offline and for nothing.

## Promotion is publication

Not storage. Say it plainly, because the two feel the same at the moment you do it and are not the
same afterwards:

- A case holds the **whole composed prompt and the whole diff** that was sent. Redaction masks
  the credential shapes it knows — named values, vendor prefixes, connection strings — and never
  masks source. A secret with no name and no prefix is a secret it cannot see.
- `git rm` is not deletion. A promoted case stays reachable in every clone, in every fork, in
  everyone's reflog, forever.
- The cap below bounds how large the permanently published set gets. It does not make any of it
  erasable.

So promotion is a pull request somebody reads, and the thing they are reading is the case, not the
diffstat. Nothing here is automated, and no agent of this repository can write here: this
repository's `lockstep.py` puts `evidence/` in tier 1 of its `PathPolicy`, so `ChangeGuard` refuses
the path at the write-tool boundary and again at `apply`. That line exists because this paragraph
used to make the claim without it — a `fix` run given a ticket mentioning the eval corpus could
have staged a case, and the `propose` job, which holds `contents: write`, would have committed it.

It is this repository's line rather than the framework's. `evidence/` is a convention, not a
framework path, and an adopter who wants an automated promotion simply does not write it.

## The cap

**24 cases per directory under `cases/`.** Past that, promoting one means retiring one, in the same
pull request, with the reason in the message. `tests/in_lockstep/test_evidence.py` enforces it, and
a 25th turns the build red rather than being quietly accepted.

The number is not load-bearing and nobody has shown that 24 discriminates between two prompts better
than 50 would. What it is for is a ceiling somebody has to argue with: an uncapped corpus grows by
accretion until nobody reads it, and a corpus nobody reads is a published prompt with no
counterpart benefit.

## What is here, and what is not

```
evidence/cases/<family>/<name>.json    one promoted case, self-contained
```

Today that is one case: `cases/review/lockstep-48-security.json`, a real model reviewing
in-lockstep/lockstep#48 through the security lens — the recording the package already ships and
`review --offline` replays, harvested from that cassette and the `request.json` beside it. It was
the first promotion because it was the one that published nothing new.

Self-contained matters (`GATE-EVAL-4`): a case carries the answer its expectations came from, so it
settles with no cassette anywhere. The tape it was harvested from was destroyed with the CI runner
that made it, and `harvested.cassette` is provenance rather than a dependency.

`cases/` holds **only** case files. `load_cases` is `rglob("*.json")` and `Case.parse` refuses any
object without an `expect`, so one stray JSON file — a downloaded cassette, a provenance note —
does not sit inertly beside the cases. It crashes `eval run` outright.

Not here, deliberately: cassettes. The tape never leaves the runner, which is enforced by where it
is written rather than promised.

## Promoting one

Five commands, on a laptop, by a person. The `mkdir` is not padding: only `review/` exists, so the
first promotion into any other family is the one where `cp` into a missing directory fails.

```bash
gh run download <run-id> -n lockstep-run -D /tmp/candidates
mkdir -p evidence/cases/review
cp /tmp/candidates/.lockstep/cases/review/<name>.json evidence/cases/review/
uv run in-lockstep eval run --corpus evidence/cases
git add evidence/cases/review/<name>.json
```

The name is yours to choose. `harvest` names a case after the first six words of the question it
was asked, which for a review is `review-the-change-between-<base>-and`; the file name is what a
person reads in `eval run`'s output, so name it for what it was.

Then open a pull request. Read the case in the diff — all of it, including the request — and say in
the message why this one is worth publishing forever. If `eval run` does not settle it, do not
promote it: a case that cannot be settled offline is a file, not a measurement.

### From the shipped recording

The package carries one recording of its own — the demo `review --offline` replays — and its
request ships beside it in `request.json`, which `harvest` reads. That is where the first case came
from, and it is the one promotion that publishes nothing new:

```bash
uv run in-lockstep eval harvest --from src/in_lockstep/cassettes/review-security.json \
  --into /tmp/candidates --family review
```

## Tightening a case

A harvested case's expectations were derived from the answer it came from, so the prompt that
produced that answer passes it by construction. That makes a promoted case a floor: `make check`
fails if a change below the model breaks it, and a prompt proposal that loses it is `regressed`.

It becomes something a prompt can *improve on* only when a person tightens it. Read the answer,
decide what a correct one would also have said, and put that in the expectations — a `contains`
naming the finding the model missed, a higher floor on `count`, a field under `schema`. That edit
is the one labelling act the learning loop learns from: `in-lockstep improve` refuses to spend on
a corpus every case of which passes, and drafts against exactly the checks a tightened case fails.

Tighten in a pull request, like promoting. The expectation is a claim about what good output looks
like, and it should be read by somebody other than the person who made it.

## Why not `.lockstep/`

`.lockstep/` leads `DENY_ALWAYS` in `core/changes.py`, checked before any grant, so nothing routed
through `ChangeGuard` can write there — which is correct for the directory holding executable
configuration and every artifact of a run, and wrong for a directory whose whole purpose is to be
committed. `.lockstep/cases/` is the candidate pile and is gitignored. This is the promoted set and
is not.

Both are denied to an agent, and for opposite reasons: `.lockstep/` because writing it changes what
a later run may do, `evidence/` because writing it publishes a prompt and a diff forever. Only one
of the two is the framework's rule.
