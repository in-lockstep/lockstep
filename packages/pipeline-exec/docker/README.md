# The exec image

```bash
docker build -f docker/Dockerfile -t ghcr.io/pipeline-fw/exec:dev .
docker push ghcr.io/pipeline-fw/exec:dev
```

Then record the digest — not the tag — in the consuming pipeline's `.pipeline/pins.lock`:

```json
{ "capabilities": { "exec": { "image": "ghcr.io/pipeline-fw/exec", "digest": "sha256:…" } } }
```

`lockstep compile` refuses to emit an unpinned image, so a rebuild can never change what an existing
pipeline runs.
