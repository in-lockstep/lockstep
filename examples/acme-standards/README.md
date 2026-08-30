# acme-standards — an organisation's standards as an installable package

Goal 9 is hierarchical inheritance: org, then team, then repository. This is the org layer, in
its entirety — a `pyproject.toml` that declares one entry point and an `apply` function that
contributes what the organisation wants everywhere.

## How it works

1. **Publish** this package to your internal index (rename `acme` to yourself throughout).
2. **Install** it in a repository — `uv add acme-lockstep`. That is the whole application step:
   `Lockstep.detect()` discovers every `in_lockstep.standards` entry point and applies it before
   the repository's own `lockstep.py` lines run. There is no import to remember, so there is no
   import to forget — which is what makes this distribution rather than convention.
3. **Read** what applied: `in-lockstep ls` prints the package under `standards`, its policy
   layers with `<- plugin:acme`, and its bindings at the `plugin` tier.

## The precedence story, honestly

- **Policy layers merge tighten-only.** The stack this contributes into takes the lowest
  ceiling, the strictest scan, the union of denied tools. A standards package can tighten every
  repository and can loosen none — and so can the repository, relative to the package.
- **Bindings sit at `Tier.PLUGIN`.** A repository's own `lockstep.bind(...)` is `Tier.EXPLICIT`
  and wins regardless of who ran first. Overriding one line of the org package is one line.
- **Removal is visible, not impossible.** A repository can uninstall the package; that is a diff
  in its dependencies that review sees. There is deliberately no environment variable that skips
  loading, and a package that fails to load stops the run rather than silently proceeding
  without the standards somebody installed.

## Teams layer the same way

A team package is this same shape with a later-sorting entry-point name — application order is
by name, so `00-acme` runs before `10-payments-team`, stated where everyone can read it. Because
the stack is tighten-only, order only decides who is printed first; neither layer can undo the
other.
