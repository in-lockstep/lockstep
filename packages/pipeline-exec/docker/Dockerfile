# The image compiled workflows run their deterministic steps in.
#
# It carries the language runtimes a `script:` step may need (chosen by file extension), the
# executors, and the browsers they drive. Generated workflows reference it by digest, never by tag,
# so a rebuild cannot change what a pinned pipeline runs.

FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy

# Node comes from NodeSource, not from the base image and not from the distribution.
#
# The Playwright *Python* image carries no `npm` — it bundles a private node binary for its own
# driver and nothing on PATH — so this used to fail at `npm install` after every apt package had
# already been fetched. And noble's own `nodejs` is 18.x, which is past end of life: a published
# image should not hand consumers an unsupported runtime for their `.ts` and `.js` steps.
#
# Runners for the script extensions the compiler dispatches on: .py, .sh, .ts, .js, .rb, .go.
# `gh` and `unzip` are used by the step-cache action to consult the durable artifact layer.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        git jq unzip ruby-full golang-go gh nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && npm install -g tsx \
    && node --version && npm --version && tsx --version

COPY . /opt/pipeline-exec
RUN pip install --no-cache-dir "/opt/pipeline-exec[browser]" && pipeline-exec --version

# Actions runs steps as root in a container job and mounts the workspace over /github/workspace.
WORKDIR /github/workspace

# Last, so it costs a metadata layer rather than invalidating every expensive one above it. GHCR
# uses this to link the package to the repository, which is what makes the published image's page
# show the source that built it and inherit its access.
LABEL org.opencontainers.image.source="https://github.com/in-lockstep/lockstep" \
      org.opencontainers.image.description="Deterministic executors for pipelines compiled by lockstep." \
      org.opencontainers.image.licenses="Apache-2.0"
