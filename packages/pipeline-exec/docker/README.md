# The executor image

The image compiled workflows run their deterministic steps in. Published from this repository by
[`release-exec-image.yml`](../../../.github/workflows/release-exec-image.yml) on an `exec-v*` tag.

```bash
git tag exec-v0.1.0 && git push origin exec-v0.1.0
```

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
