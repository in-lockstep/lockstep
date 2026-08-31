# lockstep-index — a catalog, in its entirety

An `index.toml` and the receipts it points at. That is the whole registry format: no service, no
accounts, no ranking. Publish it by committing it somewhere with a raw https URL.

```bash
in-lockstep market add acme https://raw.githubusercontent.com/acme/index/main/index.toml
in-lockstep search tdd
in-lockstep add acme-review-prompts
```

## What the catalog is for, and what it is not for

**It is read at search and accept time, never during a run.** A run of a repository that installed
a pack is identical to a run of one that vendored the same class by hand. That is what keeps a
strategy from ever being selectable by a name — which is a property `--strategy` would otherwise
have quietly returned, since a name is something a ticket body could eventually carry.

**Its receipts are falsifiable.** `receipt = ...` points at a file derived by `pack describe`, so
it records what the author's code did rather than what the author wrote. `in-lockstep add`
re-derives the same receipt from the code you installed and compares: a pack that holds more than
the catalog published is refused outright, because that is not a decision to weigh, it is a
listing that does not describe the code.

**Its criteria are not an endorsement.** Meeting them says a pack can be measured before it is
trusted. It says nothing whatever about whether the code is any good, and a catalog implying
otherwise would be transferring a judgement nobody made.

## The criteria, and why one you might expect is missing

`market lint` checks each listed pack's receipt for:

1. a receipt at all, derived by `pack describe`;
2. `imports: none` for anything listed as `kind = "prompt"`;
3. a corpus, so the pack can be measured;
4. at least one cassette, so measuring it costs nothing.

The design note's first draft had a fifth: *the layer projection retains `guardrail:baseline`*.
It is not here because it cannot be answered from a pack's receipt. A prompt body has no
projection until something composes it, and which guardrails end up around it is a property of the
repository that binds it, not of the pack. `doctor`'s `DOC171` is where that question is actually
answerable, and it is asked there against the **bound** adapters.

## This catalog fails its own criteria today, on purpose

```
$ in-lockstep market lint examples/lockstep-index/index.toml

  acme-review-prompts          prompt    FAILS
                               missing: at least one cassette, so measuring costs nothing

  1 entr(y/ies), 1 failing the criteria this catalog states
```

`examples/acme-review-prompts` ships prose and two corpus cases, and nobody has recorded a replay
of it against a real model. So it cannot be measured for nothing, so this catalog will not list it
as meeting its criteria — and a lint that always passed would teach nothing about what the check
is for.

Clearing it is `in-lockstep review --record` against a real change, once, with the resulting
cassette committed into the pack. Until somebody does that, the honest state of this entry is the
one printed above.
