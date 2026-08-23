#!/usr/bin/env python3
"""List the endpoints this pipeline writes contract tests for.

This is the deterministic half of the pipeline: it decides *what* gets tested. Keeping it a script
rather than an agent means the work list is reproducible, costs nothing, and can be reviewed in a
diff — the agent is then only asked to do the part that genuinely needs judgement.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# A curated slice of httpbin. Replace this with a read of your own OpenAPI spec, a route table, or
# whatever else names the surface you want held to a contract.
ENDPOINTS: list[dict[str, object]] = [
    {
        "key": "status-200",
        "method": "GET",
        "path": "/status/200",
        "expects": 200,
        "describes": "Returns exactly the status code requested.",
    },
    {
        "key": "status-404",
        "method": "GET",
        "path": "/status/404",
        "expects": 404,
        "describes": "Returns 404 for a requested 404, without a body contract.",
    },
    {
        "key": "json-document",
        "method": "GET",
        "path": "/json",
        "expects": 200,
        "describes": "Returns a fixed JSON document with a top-level `slideshow` object.",
    },
    {
        "key": "uuid",
        "method": "GET",
        "path": "/uuid",
        "expects": 200,
        "describes": "Returns a JSON object with a `uuid` field. The value differs every call.",
    },
    {
        "key": "post-echo",
        "method": "POST",
        "path": "/post",
        "expects": 200,
        "describes": "Echoes the posted body back under a `json` field.",
    },
    {
        "key": "headers",
        "method": "GET",
        "path": "/headers",
        "expects": 200,
        "describes": "Returns the request headers under a `headers` object.",
    },
]


def select(only: str) -> list[dict[str, object]]:
    """Narrow the surface to specific keys, so a run can target one endpoint."""
    wanted = {name.strip() for name in only.split(",") if name.strip()}
    if not wanted:
        return ENDPOINTS
    return [endpoint for endpoint in ENDPOINTS if endpoint["key"] in wanted]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--only", default="")
    args = parser.parse_args()

    endpoints = select(args.only)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(endpoints, indent=2) + "\n", encoding="utf-8")
    print(f"listed {len(endpoints)} endpoint(s) -> {output}")


if __name__ == "__main__":
    main()
