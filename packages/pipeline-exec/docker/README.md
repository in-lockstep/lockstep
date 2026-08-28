# The executor image

> **No longer published.** `release-exec-image.yml` was removed with the compiler that referenced
> this image by digest. The Dockerfile is kept because the executors it packages are still the
> only thing here that drives a real application, and an image is what they will need if they are
> ever wired to a `run` verb.

The image the deterministic steps of a compiled pipeline used to run in. It builds on the
Playwright base and adds the runners dispatched on by extension.

The release note carries the **digest**, which is the thing consumers pin:

```bash
lockstep pin --exec-digest=sha256:...
```

`lockstep compile` refuses to emit an image that is not pinned by digest, so rebuilding a tag cannot
change what an already-reviewed pipeline runs.

## Building it locally

```bash
docker build -f docker/Dockerfile -t pipeline-exec:dev packages/pipeline-exec
docker run --rm pipeline-exec:dev pipeline-exec list-commands
```

## Why the two capabilities are tagged separately

The composite actions under `actions/` and this image are released on their own tag lines —
`actions-v*` and `exec-v*` — rather than sharing the compiler's. A pin should move when the thing
behind it moved. Sharing tags would bump every consumer's lock file on every compiler release, and a
pin bump that changes nothing teaches people to approve pin bumps without reading them.
