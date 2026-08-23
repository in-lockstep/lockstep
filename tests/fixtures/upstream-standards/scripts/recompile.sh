#!/usr/bin/env bash
# Fetch the upstreams at their new commits and recompile, into a directory the proposal step
# publishes. Nothing is committed here: the recompile is a proposal, and a pipeline that could
# commit its own recompile is a pipeline whose reviewed output stopped being the artifact that runs.
set -euo pipefail

output=""
while [ $# -gt 0 ]; do
  case "$1" in
    --output=*) output="${1#*=}" ;;
  esac
  shift
done
[ -n "$output" ] || { echo "--output is required" >&2; exit 2; }

lockstep fetch
lockstep compile

mkdir -p "$output"
# The pins the recompile was made against travel with it, or a reviewer cannot tell what moved.
cp -R .github "$output/" 2>/dev/null || true
mkdir -p "$output/.pipeline"
cp .pipeline/pins.lock "$output/.pipeline/" 2>/dev/null || true

echo "recompiled at the new pins -> $output"
