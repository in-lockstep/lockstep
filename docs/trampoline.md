# The trampoline contract

**A trampoline is the CI file that invokes the CLI, and it contains no lifecycle logic.** What a
run actually does lives in `.lockstep/lockstep.py`, where it is Python that can be read, tested
and run on a laptop. The YAML holds only what the CI host owns and no Python can express: the
trigger, the job split, per-job permissions, and which secret each job holds.

This page is the contract a trampoline satisfies on *any* host. GitHub Actions and GitLab CI ship
as worked implementations, and porting to Jenkins, Tekton or anything else that can run a shell
command is a matter of mapping each clause, not of understanding the framework.

`in-lockstep init` writes the trampoline once and never reads it back: no drift check, no
`--force`. The day something compares it against a freshly generated one, it has become generated
output rather than a scaffold.

## The contract

Six clauses. Each exists for a reason a section below gives; a port that cannot satisfy one
should say so in a comment at the top of its trampoline rather than quietly weakening it.

1. **Configuration loads from a trusted ref, never from the change under review.** The framework
   enforces this itself (`.lockstep/lockstep.py` comes from the base/target branch), but the
   trampoline must pass the base ref through, and must own up to what the *host* loads from
   where. On GitLab, a merge-request pipeline runs `.gitlab-ci.yml` from the source branch: the
   change under review can edit the YAML, so the YAML must be protected at the host
   (CODEOWNERS with required approvals, or a CI config path pointing at a protected location).
2. **Authorization precedes work, and it authorizes the asker, not the text.** A chat-ops
   comment, a label, a pipeline variable: whoever supplies it is verified before anything runs,
   by a job that holds **no credential**. The decision is `in-lockstep gate`, a Python function
   with tests, not a `grep` inside a YAML `if:`. The input the asker wrote (issue text, comment
   body) stays untrusted regardless of who they are.
3. **The provider credential and the write token are never co-resident.** The job that talks to
   a model holds the provider key and read-only access; the job that writes holds a write token
   and must be *unable* to reach a model. On GitHub, that means not installing the provider
   extra at all. One process cannot hold two token scopes; that is why there are separate jobs
   rather than separate steps.
4. **What crosses between jobs is an artifact, and it is untrusted.** The unprivileged half
   stages a `ChangeSet`; the privileged half re-runs ChangeGuard over it before writing a byte,
   refuses any branch outside `in-lockstep/<workflow>/[<ticket>/]<run-id>`, and keeps the
   artifact outside the tree it commits from. Otherwise the artifact itself gets swept into the
   change.
5. **Everything that runs next to a credential is pinned.** The framework by version
   (`in-lockstep==X.Y.Z`, written by `init` as the version that wrote the scaffold), and any
   host-side actions by commit SHA where the host has them. An unpinned install feeds whatever
   the registry serves next to the job holding the key, and a floating release breaks every
   adopting repository at once with no repo-local diff to blame.
6. **Bounded, and honest when it cannot run.** Every job carries an explicit timeout (CI
   defaults are measured in hours). A missing credential, such as a fork's pull request, skips
   with a message rather than failing: a red check the contributor cannot fix teaches everyone
   to ignore red.

The contract as a sequence, comment to pull request, is [chat-ops](https://in-lockstep.github.io/lockstep/diagrams/chat-ops.html); the write jobs it protects are drawn in [implement](https://in-lockstep.github.io/lockstep/diagrams/implement.html) and [fix](https://in-lockstep.github.io/lockstep/diagrams/fix.html), the read verb in [review](https://in-lockstep.github.io/lockstep/diagrams/review.html), and how the record leaves a read-only job in [the ledger](https://in-lockstep.github.io/lockstep/diagrams/ledger.html).

## The job split, by verb

A read-only verb (review) needs **two jobs**: the review itself, with the provider key and read
access and nothing else, and a `publish` job with the write token and no provider, which pushes
the review's record and posts each lens's verdict and findings as its own sticky comment from
the bodies `--comment-out` wrote. A write-capable verb (implement, fix) needs **three**:

| Job | Credential | Access | Does |
|---|---|---|---|
| gate | none | read | `in-lockstep gate`: may this person ask? |
| work | provider key (+ tracker read) | read | `in-lockstep provision`, then `in-lockstep run <verb>/from-ticket`: builds the repository's environment, stages an artifact |
| propose | write token | write | `in-lockstep run <verb>/propose`: opens the change request |

A read-only verb asked for **on the thread** -- `/review <lens>` on a pull request -- needs three
as well, and for the same reason the write verbs do, even though nothing is written to the tree:
posting a comment is a write. `gate` authorizes the asker holding nothing. `review` holds the
provider key and `pull-requests: read`, because a comment event carries neither ref and the
framework asks the host what the pull request points at; it resolves the lens in Python against
the lenses the module binds, runs it, and writes the comment body with `--comment-out`. `post`
holds `pull-requests: write` and no provider SDK, and runs `in-lockstep comment --body-file`,
which finds the sticky comment by the marker inside the body it was handed -- so the lens, which
came out of an untrusted comment, never appears in YAML. `in-lockstep init --review` writes it.

Backport sits between the two shapes. Its default is deterministic: `git cherry-pick` stages the
artifact and no model is called, so its work job needs **no provider key at all**. The propose job
opens the artifact against the release line with `apply --base <target>`. Only `--resolve`, which
lets a model merge conflicts, makes it a spender with the full three-job shape.

The work job needs to *read* the tracker, because `from-ticket` fetches the ticket. On GitHub that
read is the workflow token with `issues: read`; on GitLab it is a read-only (`read_api`) project
access token scoped to the work environment. A read credential beside the provider key is inside
the contract; a *write* credential beside it is what clause 3 exists to prevent.

The work job also runs `in-lockstep provision` before anything else, as its own step, before
any credential is used. The framework runs from an installed interpreter with nothing of the repository's
in it, and the suite a strategy runs to prove a change needs the repository's own environment:
`uv sync --locked`, `npm ci`, whatever detection bound from a lockfile that exists
(`in-lockstep ls` shows which, and where the tool came from). The step is not `|| true`. An
environment that could not be built is the work job's failure, and it is named there rather than
as a red suite twenty minutes later; a repository with nothing to provision prints `not bound`
and the job goes on. It runs only in the work job, whose checkout is the default branch. The
review job never runs the suite, and on a pull request its checkout is the change under review,
whose install hooks must not run beside a token. The propose job commits what is in its tree,
and an install writes into it. On GitHub the step holds no credential at all; on GitLab the
job's variables are in scope for every script line, and on both hosts it is the sandbox's
environment allowlist that keeps the key out of a lockfile's install hook. A repository that
runs the framework from its own checkout, `uv sync` then `uv run in-lockstep` as this
repository's own workflows do, has provisioned with that sync; the scaffold's separate step
exists because `uvx`'s interpreter holds nothing of the repository's.

Human approval slots between work and propose where the host can express it: a GitHub
`environment` with required reviewers, a GitLab protected environment with required approvers.
With no protection rules configured, the change request itself is the review. The framework opens
AI changes as drafts and marks them ready only when their tests passed, so nothing red lands in a
human's queue either way.

## The two shipped mappings

| Contract clause | GitHub Actions | GitLab CI |
|---|---|---|
| Trigger, read verb | `on: pull_request` | `rules: $CI_PIPELINE_SOURCE == "merge_request_event"` |
| Trigger, read verb on request | `issue_comment` (`/review <lens>`); the lens is resolved in Python against the lenses the module binds, the pull request's refs are asked of the host, and a job holding the write token and no provider SDK posts the body the reviewing job wrote | none: GitLab CI has no issue-comment trigger. The review job runs on every merge request; a lens on request is `in-lockstep review --aspect <lens>` locally, or a pipeline run with variables driving the same command |
| Trigger, write verb | `issue_comment` (`/implement`, `/fix`), plus `issues: labeled` (`ai-generated`). The comment trigger fires on an **issue or a pull request**, since a reviewer asks for the next attempt where they are reading; the comment's number is passed through and `ticket_for` resolves it, never an `if:` expression | run-pipeline-with-variables (`LOCKSTEP_ISSUE`, and `LOCKSTEP_VERB=fix` for the fixing verb; `implement` otherwise), manually or via the trigger API, on one gate/work/propose pair. GitLab CI has no issue-comment or issue-label trigger; a webhook bridge can supply one |
| Trigger, learning loop | `schedule:` in `improve.yml` (this repository's own file; no scaffold writes it), `improve/measure --record --blocked-ok` then `improve/propose` on the propose split | a pipeline schedule on the default branch setting `LOCKSTEP_IMPROVE=1`; the `measure` and `improve-propose` jobs run the same two commands |
| Base ref through | `origin/${GITHUB_BASE_REF}` | `origin/${CI_MERGE_REQUEST_TARGET_BRANCH_NAME}`, after an explicit `git fetch` (MR pipelines do not fetch the target branch) |
| Who asked | `github.event.comment.user.login` + `author_association`, verified by the gate job | `GITLAB_USER_LOGIN`; no `author_association` exists, so the gate answers from CODEOWNERS, read from a checkout the job `rules:` pin to the default branch, because the asker picks the pipeline's ref |
| Credential scoping | per-job `permissions:` + step-scoped `secrets`, enforced by the file itself | environment-scoped variables (`lockstep-work`: provider key + `read_api` token; `lockstep-propose`: the write-capable token). **Weaker, and operator-contingent:** GitLab's default variable scope is every environment, so an unscoped variable silently puts both credentials in both jobs. The file cannot enforce the split, only the variable configuration can |
| Review conversation, read | `pull-requests: read` on the work job; `gh pr view --json comments,reviews` plus `pulls/{n}/comments` for the notes pinned to a line | `read_api` on the work environment's token; `merge_requests/{iid}/notes`, which mixes both and is told apart by whether a note carries a `position` |
| Approval gate | `environment:` with required reviewers | protected environment with required approvers |
| Artifact between halves | `upload-artifact`/`download-artifact`, downloaded to `$RUNNER_TEMP` (outside the workspace) | `artifacts:` between stages, `mv`d out of the workspace before propose runs |
| Run record out of a read-only job | the review job bundles its `lockstep-history` commits (`history --bundle`) into the `lockstep-run` artifact; a `publish` job holding `contents: write` and no provider SDK absorbs and pushes it (`history --from-bundle --push`); a scheduled `reconcile` sweep runs `history --from-artifacts lockstep-run --push` for what a cancelled run left behind, once per artifact, and `report --scm` counts what is still outstanding | the review job bundles the same way into its `artifacts:`, and a `publish` stage holding the push token (scoped to `lockstep-publish`) and no provider absorbs it on every merge request, whatever review said. No sweep: GitLab job artifacts are not listed by name across pipelines, so `report --scm` prints a dash there |
| Pinning | `==version` + action SHAs | `==version` (images may additionally be pinned by digest) |
| Repository environment | `in-lockstep provision` as its own step in the work job, before `doctor` | the same line in the work job's `script`, on an image that carries `uv` |
| Fork safety | step `if: secrets.X != ''` skips with a message | protected variables are absent on fork MRs; the script branch says so and exits 0 |

`.github/workflows/implement.yml` is the GitHub worked example. A test holds every shell statement
in it to an allowlist (invoke the framework, move the evidence, nothing else), so a `git commit` or
a `gh pr create` appearing in the YAML fails CI: each of those has a port behind it, and reaching
for the command is how lifecycle logic gets back into YAML.

The scaffolded `.gitlab-ci.yml` (from `in-lockstep init` on a GitLab repository) is the GitLab
worked example, and carries the same four jobs: review, and the gate/work/propose split for the
write verbs. They ship active rather than commented out — every one is held inert by its `rules:`
until a pipeline runs with `LOCKSTEP_ISSUE` set on the default branch, so a repository that never
asks never runs them, and an adopter who does ask is scoping credentials rather than editing YAML
the framework does not regenerate.

Keyless CI is `id_tokens:` on the `work` job, which mints a short-lived JWT into
`ANTHROPIC_IDENTITY_TOKEN` — the name the provider SDK's own jwt-bearer chain reads, so the
framework hands that case straight to it and there is no GitLab-specific resolver. What this
repository cannot tell you is whether a federation rule can be created for a GitLab issuer: that
is a question for the provider, and this repository is GitHub-hosted, so it has never performed
the exchange. The scaffold says so at the lines themselves, and a scoped protected variable
holding a long-lived key is the documented alternative rather than a fallback.

## Porting to another host

Jenkins, Tekton, Buildkite: anything that can run a shell command and pass a file between two
isolated executions can carry this contract. The port is one credential-less execution running
`in-lockstep gate`, one holding only the provider key running `in-lockstep provision` and then
`<verb>/from-ticket`, one holding
only a write token running `<verb>/propose`, an artifact handed between them that stays out of
the working tree, a pinned install, a timeout, and a graceful skip when the key is absent.

The same CLI commands, in the same order, with the same arguments. The trampoline is the only
part that changes, which is the point of the design: the process already ran on a laptop before
it ever ran in CI.
