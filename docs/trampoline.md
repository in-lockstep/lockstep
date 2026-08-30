# The trampoline contract

**A trampoline is the CI file that invokes the CLI, and it contains no lifecycle logic.** What a
run actually does lives in `.lockstep/lockstep.py`, where it is Python that can be read, tested
and run on a laptop. The YAML holds only what the CI host owns and no Python can express: the
trigger, the job split, per-job permissions, and which secret each job holds. This page is the
contract a trampoline satisfies on *any* host — GitHub Actions and GitLab CI ship as worked
implementations, and porting to Jenkins, Tekton or anything else that can run a shell command is
a matter of mapping each clause, not of understanding the framework.

`in-lockstep init` writes the trampoline once and never reads it back: no drift check, no
`--force`. The day something compares it against a freshly generated one, it has become generated
output rather than a scaffold.

## The contract

Six clauses. Each exists for a reason a section below gives; a port that cannot satisfy one
should say so in a comment at the top of its trampoline rather than quietly weakening it.

1. **Configuration loads from a trusted ref, never from the change under review.** The framework
   enforces this itself (`.lockstep/lockstep.py` comes from the base/target branch), but the
   trampoline must pass the base ref through — and must own up to what the *host* loads from
   where. On GitLab, a merge-request pipeline runs `.gitlab-ci.yml` from the source branch: the
   change under review can edit the YAML, so the YAML must be protected at the host
   (CODEOWNERS with required approvals, or a CI config path pointing at a protected location).
2. **Authorization precedes work, and it authorizes the asker, not the text.** A chat-ops
   comment, a label, a pipeline variable — whoever supplies it is verified before anything runs,
   by a job that holds **no credential**. The decision is `in-lockstep gate`, a Python function
   with tests, not a `grep` inside a YAML `if:`. The input the asker wrote (issue text, comment
   body) stays untrusted regardless of who they are.
3. **The provider credential and the write token are never co-resident.** The job that talks to
   a model holds the provider key and read-only access; the job that writes holds a write token
   and must be *unable* to reach a model — on GitHub, by not installing the provider extra at
   all. One process cannot hold two token scopes; that is why there are separate jobs rather
   than separate steps.
4. **What crosses between jobs is an artifact, and it is untrusted.** The unprivileged half
   stages a `ChangeSet`; the privileged half re-runs ChangeGuard over it before writing a byte,
   refuses any branch outside `in-lockstep/<workflow>/<run-id>`, and keeps the artifact outside
   the tree it commits from — or the artifact itself gets swept into the change.
5. **Everything that runs next to a credential is pinned.** The framework by version
   (`in-lockstep==X.Y.Z`, written by `init` as the version that wrote the scaffold), and any
   host-side actions by commit SHA where the host has them. An unpinned install feeds whatever
   the registry serves next to the job holding the key, and a floating release breaks every
   adopting repository at once with no repo-local diff to blame.
6. **Bounded, and honest when it cannot run.** Every job carries an explicit timeout (CI
   defaults are measured in hours). A missing credential — a fork's pull request — skips with a
   message rather than failing: a red check the contributor cannot fix teaches everyone to
   ignore red.

## The job split, by verb

A read-only verb (review) needs **one job**: provider key, read access, nothing else. A
write-capable verb (implement, fix) needs **three**:

| Job | Credential | Access | Does |
|---|---|---|---|
| gate | none | read | `in-lockstep gate` — may this person ask? |
| work | provider key (+ tracker read) | read | `in-lockstep run <verb>/from-ticket` — stages an artifact |
| propose | write token | write | `in-lockstep run <verb>/propose` — opens the change request |

Backport sits between the two shapes. Its default is deterministic — `git cherry-pick` stages the
artifact, no model — so its work job needs **no provider key at all**, and the propose job opens
the artifact against the release line with `apply --base <target>`. Only `--resolve`, which lets a
model merge conflicts, makes it a spender with the full three-job shape.

The work job needs to *read* the tracker — `from-ticket` fetches the ticket — which on GitHub is
the workflow token with `issues: read` and on GitLab is a read-only (`read_api`) project access
token scoped to the work environment. A read credential beside the provider key is inside the
contract; a *write* credential beside it is what clause 3 exists to prevent.

Human approval slots between work and propose where the host can express it: a GitHub
`environment` with required reviewers, a GitLab protected environment with required approvers.
With no protection rules configured, the change request itself is the review — the framework
opens AI changes as drafts and marks them ready only when their tests passed, so nothing red
lands in a human's queue either way.

## The two shipped mappings

| Contract clause | GitHub Actions | GitLab CI |
|---|---|---|
| Trigger, read verb | `on: pull_request` | `rules: $CI_PIPELINE_SOURCE == "merge_request_event"` |
| Trigger, write verb | `issue_comment` (`/implement`), `issues: labeled` (`ai-generated`) | run-pipeline-with-variables (`LOCKSTEP_ISSUE`), manually or via the trigger API — GitLab CI has no issue-comment trigger; a webhook bridge can supply one |
| Base ref through | `origin/${GITHUB_BASE_REF}` | `origin/${CI_MERGE_REQUEST_TARGET_BRANCH_NAME}`, after an explicit `git fetch` (MR pipelines do not fetch the target branch) |
| Who asked | `github.event.comment.user.login` + `author_association`, verified by the gate job | `GITLAB_USER_LOGIN`; no `author_association` exists, so the gate answers from CODEOWNERS — read from a checkout the job `rules:` pin to the default branch, because the asker picks the pipeline's ref |
| Credential scoping | per-job `permissions:` + step-scoped `secrets` — enforced by the file itself | environment-scoped variables (`lockstep-work`: provider key + `read_api` token; `lockstep-propose`: the write-capable token). **Weaker, and operator-contingent:** GitLab's default variable scope is every environment, so an unscoped variable silently puts both credentials in both jobs — the file cannot enforce the split, only the variable configuration can |
| Approval gate | `environment:` with required reviewers | protected environment with required approvers |
| Artifact between halves | `upload-artifact`/`download-artifact`, downloaded to `$RUNNER_TEMP` (outside the workspace) | `artifacts:` between stages, `mv`d out of the workspace before propose runs |
| Pinning | `==version` + action SHAs | `==version` (images may additionally be pinned by digest) |
| Fork safety | step `if: secrets.X != ''` skips with a message | protected variables are absent on fork MRs; the script branch says so and exits 0 |

`.github/workflows/implement.yml` is the GitHub worked example — a test holds every shell
statement in it to an allowlist (invoke the framework, move the evidence, nothing else), so a
`git commit` or a `gh pr create` appearing in the YAML fails CI: each of those has a port behind
it, and reaching for the command is how lifecycle logic gets back into YAML. The scaffolded `.gitlab-ci.yml` (from `in-lockstep init` on a GitLab repository) is the
GitLab worked example: one active review job, and the gate/work/propose block commented out until
its environments and credentials are provisioned.

## Porting to another host

Jenkins, Tekton, Buildkite — anything that can run a shell command and pass a file between two
isolated executions can carry this contract. The port is: one credential-less execution running
`in-lockstep gate`, one holding only the provider key running `<verb>/from-ticket`, one holding
only a write token running `<verb>/propose`, an artifact handed between them that stays out of
the working tree, a pinned install, a timeout, and a graceful skip when the key is absent. The
same CLI commands, in the same order, with the same arguments — the trampoline is the only part
that changes, which is the point of the design: the process already ran on a laptop before it
ever ran in CI.
