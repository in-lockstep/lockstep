# httpbin contract pipeline

The worked example from [the getting started guide](../../docs/getting-started.md). It holds six
endpoints of [httpbin.org](https://httpbin.org) to a contract: a script lists the surface, an agent
writes one test per endpoint, the tests run against the live service, and the report is published to
a long-lived branch.

```bash
lockstep lint --root .      # is the spec well built?
lockstep doctor --root .    # will GitHub accept it?
lockstep compile --root .   # generate the workflows
```

One AI step, five deterministic ones. Once the generated tests are reviewed and merged, a scheduled
run costs zero credits.
