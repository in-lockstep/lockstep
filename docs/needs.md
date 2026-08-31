# What the people who would adopt this need next

Derived from asking who would put this in front of an organization, what they arrive wanting, and
what would make them put it down — rather than from reading the code. Five people: the platform
engineer who owns the paved road, the security lead who signs off before agents touch anything
real, the engineering leader who funds it, the staff engineer who builds the pipelines, and the
small team who will author nothing and wants the loop working by Friday.

The list predates the pivot from a compiler to a framework. It is kept because most of it survived
that change unaltered — which is itself the useful signal: the needs were about adoption, and
adoption did not care which architecture served it.

| | Need | Who | Status |
|---|---|---|---|
| **N1** | Publish the capabilities | everyone | **moot** — the composite actions and the exec image belonged to the compiler and went with it. What replaces them is a CLI and a twelve-line trampoline. |
| **N2** | Run the loop on this repository | everyone | **closed in principle, thin in fact.** The framework validates and tests itself in-process, `review` runs the whole path offline, and live model calls have now happened — `/implement` and `/fix` have both run from a comment, and the ledger carries their records. What is still missing is volume: a handful of runs is not a merge rate, and N3 stays open. |
| **N3** | A measured time to first value | leader, small team | open. `init` writes two files; nobody has timed the path from that to a first useful review. |
| **N4** | A shorter first day | small team | **improved.** The first day used to be a spec tree of seven directories. It is now `init`, then editing one Python file. |
| **N5** | A way to tell a working judge from a broken one | small team | open, and now visible: `eval report` says 27 outstanding rather than reporting a pass rate, so a suite with no judge announces itself. |
| **N6** | Aggregation across repositories | platform, security, leader | deferred to post-1.0 with workspaces. Still no demand evidence. |
| **N7** | Migration across capability majors | platform | **closed by decision.** No importer; 0.x specs are frozen and `in-lockstep==0.1.0` stays installable. Defensible only because there were no adopters — see ADR 0001. |
| **N8** | Outcome metrics, not pipeline metrics | leader | partially met. Every outcome carries a `Cost`, and `decided` is a metric dimension so a run that decided nothing cannot read as a success. Cost-per-merged-change needs the eval loop. |
| **N9** | Transcript retention as a supported decision | security, author | open. Cassettes record whole prompts and tool results; they pass through redaction, but how long a repository keeps them is undecided. |
| **N10** | A controls crosswalk | security | **done** — [`controls-crosswalk.md`](controls-crosswalk.md), and it says which control was lost rather than replaced. |
| **N11** | An inner loop for prompt iteration | author | **done** — `show-prompt` renders the composition offline with no key, and `--offline` replays a cassette. |
| **N12** | A quick reference for the layer taxonomy | author | **partly moot.** The compiler's three prompt layers are now guardrails, body, skills and contexts, composed in `Prompt.system` and frozen in the characterization corpus. |
| **N13** | An entry surface that routes by persona | everyone | **done** — the site at https://in-lockstep.github.io/lockstep/ routes by accountability rather than by feature: five roles, each with what the framework changed for them, plus a walkthrough that adopts the framework into a sample library and moves one change through the whole lifecycle. Terminal output on it is captured verbatim; quotes are attributed by role. |
| **N14** | Publish the Python distributions | everyone | **done** for 0.1.x. 1.0 reuses the name for a different product, which ADR 0001 records as a deliberate and slightly uncomfortable decision. |

## The one that matters most

N2. Nothing has executed against a real model. The framework runs, the pipeline is wired end to
end, and the ledger writes a line — but with a stub on the other end of it. Until a real call
happens the project has architecture and no evidence, which is the same position it was in before
the pivot, reached by a different route.

That is why the plan's abandon criteria are weighted the way they are: a time tripwire and a cost
tripwire can tell you a build has stalled or is uneconomic, but only running it against real
changes can tell you whether the output is worth reading.
