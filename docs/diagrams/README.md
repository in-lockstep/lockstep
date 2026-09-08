# Diagrams

One interactive page per shipped surface and per design topic, served from the site at
https://in-lockstep.github.io/lockstep/diagrams/ and authored as the Archify JSON in this
directory. Every page has pan and zoom, search, a light and dark theme, and export. Regenerate
one after editing its `.json` with the Archify skill's `deliver` command, then copy the HTML to
the `diagrams/` directory of the `gh-pages` branch.

| Page | What it shows |
|---|---|
| [runtime-architecture](https://in-lockstep.github.io/lockstep/diagrams/runtime-architecture.html) | The runtime: CLI, loader, run context, container, middleware, AI and deterministic adapters, the invoker and registry, the privileged tier, and where evidence goes. |
| [implement](https://in-lockstep.github.io/lockstep/diagrams/implement.html) | `implement/from-ticket`, `implement/propose`, `implement/report`: the two-job split and what stops a run. |
| [implement-strategies](https://in-lockstep.github.io/lockstep/diagrams/implement-strategies.html) | `Oneshot` and `TDD`: one session, or red then green, both staging into a `ChangeSet`. |
| [fix](https://in-lockstep.github.io/lockstep/diagrams/fix.html) | `fix/from-ticket` through `DiagnoseThenFix`: reproduce, confirm red, fix, confirm green, propose. |
| [review](https://in-lockstep.github.io/lockstep/diagrams/review.html) | `review/all-lenses`: one fan-out over every bound lens, one comment per lens, one record. |
| [single-turn-verbs](https://in-lockstep.github.io/lockstep/diagrams/single-turn-verbs.html) | `AiReview`, `AiTriage`, `AiRfe`: gather, compose, one turn through the invoker, settle, check. |
| [improve](https://in-lockstep.github.io/lockstep/diagrams/improve.html) | `improve/measure`, `improve/propose`, `improve/after-review`: the loop that reads the record, drafts, measures, judges and parks on a review. |
| [judge](https://in-lockstep.github.io/lockstep/diagrams/judge.html) | `judge/corpus` and `AiJudge`: deterministic first, replay from the sidecar, route and price, one turn per ask. |
| [deterministic-verbs](https://in-lockstep.github.io/lockstep/diagrams/deterministic-verbs.html) | Detection and the verbs a repository's own commands serve: provision, test, validate, build, run, backport. |
| [backport](https://in-lockstep.github.io/lockstep/diagrams/backport.html) | Plain `cherry-pick -x`, and a contained model resolution only on a conflict. |
| [chat-ops](https://in-lockstep.github.io/lockstep/diagrams/chat-ops.html) | A `/implement` comment as a sequence: the gate job with no credential, the model job, the artifact, the write job, the pull request. |
| [security-model](https://in-lockstep.github.io/lockstep/diagrams/security-model.html) | Where secrets are and are not: trusted config ref, provenance-tagged context, injection scan, egress probe, sandbox, guard, redaction, the write job. |
| [ledger](https://in-lockstep.github.io/lockstep/diagrams/ledger.html) | A run record's path: local orphan branch or bundle, push or absorb or sweep, `origin/lockstep-history`, and every reader. |
| [prompt-composition](https://in-lockstep.github.io/lockstep/diagrams/prompt-composition.html) | Shipped, standards-package and module sources into guardrails, body, skills and contexts; what the composed text keys. |
| [human-boundaries](https://in-lockstep.github.io/lockstep/diagrams/human-boundaries.html) | `ctx.park` on a shared store, the marker on the pull request, the person's act, the tick, the continuation; what a LOCAL store refuses. |
| [learning-loop](https://in-lockstep.github.io/lockstep/diagrams/learning-loop.html) | Cassette to harvest to candidate to promoted case, then `eval run`, `improve/measure`, the proposal and `report --around`. |
| [extension-resolution](https://in-lockstep.github.io/lockstep/diagrams/extension-resolution.html) | The four bands (shipped, plugin, module, call site), the tighten-only policy exception, and how a pack is described, accepted and measured. |
