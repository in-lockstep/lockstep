# The image compiled workflows run their deterministic steps in.
#
# It carries the language runtimes a `script:` step may need (chosen by file extension), the
# executors, and the browsers they drive. Generated workflows reference it by digest, never by tag,
# so a rebuild cannot change what a pinned pipeline runs.

FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy

# Runners for the script extensions the compiler dispatches on: .py, .sh, .ts, .js, .rb, .go.
# `gh` and `unzip` are used by the step-cache action to consult the durable artifact layer.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git jq unzip ruby-full golang-go gh \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && npm install -g tsx

COPY . /opt/pipeline-exec
RUN pip install --no-cache-dir "/opt/pipeline-exec[browser]" && pipeline-exec --version

# Actions runs steps as root in a container job and mounts the workspace over /github/workspace.
WORKDIR /github/workspace
