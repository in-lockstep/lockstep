# Controls crosswalk

Moving model invocation into this process removed an execution substrate that was doing more than
running an agent. It was an egress firewall, an out-of-process spend ceiling, and a privilege split
between the process that talks to a model and the process that can write.

This table is the honest accounting. A control is **replaced** only where something now performs
the same function; where it is weaker or gone, it says so. The intent is that nobody discovers a
missing control by having an incident, and that nobody believes a control is present because a
framework type shares its vocabulary.

## What the substrate provided

| # | Control | Was | Now | Status |
|---|---|---|---|---|
**Status discipline.** A row that says *Replaced* means the replacement runs. Four rows below said
*Replaced* while the mechanism they named had no caller on any live path — the same failure
`design/gates.md` now tracks with its `unit only` status. Row 1 has since been wired and is a
control again; rows 4, 14 and 15 say what is still missing. Consult that file's status column
before citing any row here as evidence.

| 1 | **Egress firewall** | A proxy with an explicit ~55-domain allowlist, enforced outside the agent process | `EgressPolicy` — mandatory on write/execute/network capability, on a restricted repository, **or on any untrusted content in the package**; probe-verified rather than attested | **Replaced, narrower allowlist, and narrower again than the substrate in one direction.** `AiInvoker.run` calls `EgressPolicy.check()` before the first model call, so a run carrying untrusted content with no enforcement in effect is refused rather than reviewed. One deliberate suppression: a cassette or dry-run provider declares `transmits = False`, which lifts the *untrusted-content* trigger only — a tool that writes, executes or reaches the network still forces enforcement offline, because those run locally whether or not the model call is real. Demanding a firewall for a run that cannot put a byte on the wire would teach people to disable the control on their laptops. The allowlist is computed from resolved provider endpoints, declared MCP endpoints and the telemetry host, so it is tighter than the substrate's bundle — which also means package registries are *not* reachable by default. Decide that before an `EXECUTES_CODE` step needs `pip install`, not during one. |
| 2 | **Spend ceiling, per run** | An API proxy enforcing a credit cap outside the agent's process | `Spend`, checked predictively before every turn inside the invocation | **Replaced, weaker by construction.** It is enforced *inside* the process holding the API key. A bug in that process is a bug in the ceiling. |
| 3 | **Spend ceiling, per day, per agent** | Enforced by the substrate *before a run started*, per agent workflow per day | Provider-side organisation limit, attested via `doctor` (`DOC101`) | **Lost, and recorded as lost.** The replacement enforces out-of-process, which is stronger — but it replaces a per-repository-per-day partition with an org-wide pool, so one runaway repository can consume every other consumer's budget. An earlier draft of the plan called this "strictly stronger"; that was wrong and was withdrawn. |
| 4 | **Budget is mandatory** | A missing budget was a compile-time **error**; shipping an agent without one was impossible | Startup refusal (`GATE-BUDGET-1`) | **Replaced.** `Lockstep.context()` raises `UndeclaredBudget` when a bound adapter declares `SPENDS_BUDGET` and no ceiling is declared. Narrower than the original in one direction and wider in another: it does not fire for a lifecycle that only runs tests, and it does fire for any adapter that spends rather than only for a compiled agent. `ls` deliberately still works without a budget, so the diagnostic that names what spends survives the refusal that mentions it. |
| 5 | **Wall clock** | `timeout-minutes: 20` on every generated job | `InvokePolicy.deadline_seconds`, re-checked each turn, plus `timeout-minutes` in the scaffolded trampoline | **Replaced.** Note the CI default without it is 360 minutes, not 20. |
| 6 | **Privilege split** | Safe-outputs: the agent job never held a write token; a separate privileged job applied structured output | Two-job trampoline — `run` holds the provider credential and `contents: read`; `apply` holds `contents: write` and no provider credential | **Replaced.** |
| 7 | **Write scope** | A token scoped to one branch namespace | Ambient repository token (a deliberate choice, for zero-setup adoption) | **Weaker.** §6.2's "protected branches are structurally unreachable" becomes conventional. Compensated by: the guard runs a third time inside `apply`; `open_change` only ever targets the run-scoped prefix; and `doctor` **fails** when branch protection is absent (`DOC121`), because that is now the only backstop. |
| 8 | **Agent permissions floor** | `permissions: {}` at workflow level, read-only scopes re-asserted after every overlay | The `run` job requests `contents: read` and nothing else | **Replaced.** |
| 9 | **Workflow-file provenance** | *"The workflow that runs is the one on your default branch. A fork cannot modify the workflow that reviews it."* | Trusted config ref — `.lockstep/lockstep.py` and `.lockstep/` load from the base ref | **Replaced, after being silently inapplicable in CI for its first real run.** The design hazard was caught in review: "config is code, discovered at the repository root" silently means the pull request supplies the file defining every control that reviews it. The *implementation* hazard was not, and is worth recording because it is a different shape. `GITHUB_BASE_REF` is a bare branch name, an `actions/checkout` working directory is a detached HEAD with no local branch of that name, and `git show main:lockstep.py` therefore failed for a reason unrelated to the file — returning the same `None` as "this repository has no configuration", falling through to detected defaults, and running a review with none of the repository's bindings, policy or egress decisions. A control can be correctly designed, correctly reviewed, wired to a live path, and still not apply. Fixed in #57: remote-tracking refs are tried, an unresolvable ref refuses instead of returning `None`, and the CLI prints which ref configuration came from — because the reason this survived is that nothing said. |
| 10 | **MCP tool deny floor** | A guardrail's deny list was a floor an overlay could not widen; re-asserted at compile time | `PolicyStack` — union-merged denies, no removal API; `ToolSet` is the dispatch table | **Replaced for merge semantics, weaker for enforcement.** See 12. |
| 11 | **Prompt-injection scan** | `scan-input` before an agent read anything, `warn` or `block` | Scans untrusted context items before the first call, **and every tool result per turn** | **Replaced and extended.** Tool results were not covered before. |
| 12 | **Sealed standards** | A sealed guardrail reached every agent unnamed, could not be excluded, and its name could not be shadowed — refused at compile time | `PolicyStack`, append-only, monotone merge, plus `doctor --strict` in a required check | **Weaker: visibility of removal, not impossibility.** A middleware chain cannot bound code that never calls `ctx.do`. The project's own documentation already said sealing "is not an access control against the repository's own owners"; this makes that explicit rather than newly true. |
| 13 | **Sandboxed execution** | `--cap-drop=ALL --security-opt=no-new-privileges` on every executor job | `Sandbox`: container where available, otherwise a subprocess with every credential stripped | **Replaced where Docker is present; weaker otherwise.** The fallback guarantees the child cannot read a credential from the parent, and says it is not a kernel sandbox rather than implying it is. |
| 14 | **Protected paths** | `apply_patch` refused writes to CI, guardrail and agent directories | `ChangeGuard`, two tiers, enforced at the write-tool boundary **and** `open_change` **and** `apply` | **Replaced and extended.** All three named enforcement points are real and asserted against one shared list of protected paths (`GATE-GUARD-1`): the in-loop tool boundary, where a refusal is returned to the model as a tool result rather than an exception so it can choose differently inside its budget; `apply-inline`, the local default; and `apply --from-artifact`, which treats what a previous job produced as untrusted. A staged write never touches the disk, so an interrupted loop leaves no half-written tree. `.lockstep/lockstep.py` now leads tier 1, which is new and necessary: configuration is executable, so an agent that edits it can rebind any adapter and grant itself tools. |
| 15 | **Secret redaction** | Applied to prompts and logs | Privileged sink filter, default-deny over every writer, seeded by `Auth` at mint time | **Replaced and strengthened.** `GATE-REDACT-1` now holds: every module is AST-scanned for raw write primitives, and each must route through `privileged/sink.py` or carry a written reason. Standard output is covered by wrapping the stream at CLI entry, so the sink most easily forgotten — public CI logs — is the one nobody has to remember. Seeding from `Auth` rather than the environment is what makes a federated short-lived token redactable at all. |

## What has no substrate equivalent

- **A repository can import a provider directly** and bypass the policy stack, the spend
  accumulator and the egress check entirely. This is unrecoverable in a library architecture. It is
  recorded here rather than mitigated, because the alternative is implying a guarantee that a
  library cannot make.
- **A cancelled model call may still be billed.** The framework bounds wall clock and process
  state, not spend-on-cancel, so the ledger under-reports cancelled-call cost.
- **An allowlist cannot stop exfiltration through an allowed destination.** The SCM host is
  necessarily reachable, so a tool that can post to it can carry data out.
- **`doctor` verifies attestations, not the things attested.** It cannot read a provider console.

## What must be true before an unattended run

1. `IN_LOCKSTEP_ORG_SPEND_LIMIT` attested (`DOC101`).
2. Branch protection on the default branch (`DOC121`).
3. `IN_LOCKSTEP_EGRESS=enforced` where the host constrains egress (`DOC130`), and the probe agrees.
4. A base ref, so configuration does not resolve from the change under review (`DOC110`).
5. The two-job trampoline, so the provider credential and the write token are never co-resident.
