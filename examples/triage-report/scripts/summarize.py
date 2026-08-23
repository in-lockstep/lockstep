#!/usr/bin/env python3
"""Count the backlog before any model reads it.

Counting is not judgement. Doing it here means the numbers in the published report are arithmetic
rather than something a model produced — which matters, because a report whose totals cannot be
trusted is a report nobody acts on. The agent is then free to do the part that needs judgement:
saying what the numbers mean.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STALE_DAYS = 14


def age_days(timestamp: str, now: datetime) -> int | None:
    """Days since a tracker timestamp, or None when it cannot be read."""
    if not timestamp:
        return None
    cleaned = timestamp.replace("Z", "+00:00")
    for candidate in (cleaned, cleaned[:19] + "+00:00"):
        try:
            return (now - datetime.fromisoformat(candidate)).days
        except ValueError:
            continue
    return None


def summarize(issues: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    ages = {issue["key"]: age_days(issue.get("created", ""), now) for issue in issues}
    dated = {key: age for key, age in ages.items() if age is not None}
    stale = sorted(
        (key for key, age in dated.items() if age >= STALE_DAYS),
        key=lambda key: dated[key],
        reverse=True,
    )
    return {
        "total": len(issues),
        "by_type": dict(Counter(issue.get("type", "unknown") for issue in issues).most_common()),
        "by_priority": dict(Counter(issue.get("priority", "unset") for issue in issues).most_common()),
        "by_component": dict(
            Counter(
                component for issue in issues for component in (issue.get("components") or ["unassigned"])
            ).most_common()
        ),
        "unlabelled": [issue["key"] for issue in issues if not issue.get("labels")],
        "no_component": [issue["key"] for issue in issues if not issue.get("components")],
        "stale": stale,
        "stale_threshold_days": STALE_DAYS,
        "oldest_days": max(dated.values(), default=0),
        "undated": [key for key, age in ages.items() if age is None],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    issues = json.loads(Path(args.input).read_text(encoding="utf-8"))
    summary = summarize(issues, datetime.now(UTC))

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"summarized {summary['total']} issue(s) -> {output}")


if __name__ == "__main__":
    main()
