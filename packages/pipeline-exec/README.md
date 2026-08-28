# pipeline-exec

> **Orphaned as of `in-lockstep` 1.0.** Nothing in the framework imports this package.
>
> It was built as the deterministic half of a *compiled* pipeline, and most of its command surface
> — `fanout`, `shard-run`, `cache-key`, `meter`, `eval-*`, `parse-command`, `scan-input` — was glue
> a compiler emitted as literal text. That compiler was deleted, along with the contract test that
> kept the two in step, so those commands now describe a system that does not exist.
>
> What is genuinely durable here is a different thing entirely: `executors/` drives a real browser,
> a real API and a real shell against a running application, and carries resilience behaviour
> earned against one — 409 and 422 recovery, method fallback, rate-limit ladders, browser
> auto-login and crash recovery. `builtins/test_runner.py`, `builtins/discovery.py` and `reports/`
> sit on top of it. That is the `run` verb's territory, and the framework has not built it yet.
>
> So the package is kept rather than deleted, and its status is recorded rather than left to be
> discovered. Reducing it to the durable half is a decision that has not been made.
