# pr-review pipeline

The worked example from [the review guide](../../docs/reviewing-pull-requests.md). Comment
`/review security intent` on a pull request and each aspect produces its own review.

Asking again after nothing has changed does nothing. Asking again after new commits revises the
existing review in place, saying what those commits resolved and what they raised.

```bash
lockstep lint --root .
lockstep doctor --root .
lockstep compile --root .

cd extensions && uv run --with-editable . python -m pytest tests -q
```

Adding a review lens is adding a file to `aspects/`. The reviewing agent has no tools and no write
permission; one job at the end posts what it produced.
