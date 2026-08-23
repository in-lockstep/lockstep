#!/usr/bin/env python3
"""Render the plan as the markdown a reviewer will read on the pull request.

Deterministic on purpose. The planning agent decided *what* the plan says; how it is presented is
formatting, and formatting that varies between runs makes the plan comment churn for no reason —
which matters because the comment is updated in place across many iterations.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def render(plan: dict) -> str:
    lines = [plan.get("summary", "").strip(), ""]

    if plan.get("approach"):
        lines += ["### Approach", "", plan["approach"].strip(), ""]

    if plan.get("rejected"):
        lines += ["### Considered and rejected", ""]
        lines += [f"- **{item.get('option', '')}** — {item.get('reason', '')}" for item in plan["rejected"]]
        lines += [""]

    if plan.get("changes"):
        lines += ["### Files this changes", "", "| File | Why |", "|---|---|"]
        lines += [f"| `{c.get('path', '')}` | {c.get('reason', '')} |" for c in plan["changes"]]
        lines += [""]

    if plan.get("verification"):
        lines += ["### How this is proven", "", plan["verification"].strip(), ""]

    if plan.get("risks"):
        lines += ["### What this could break", ""]
        lines += [f"- {risk}" for risk in plan["risks"]]
        lines += [""]

    if plan.get("open_questions"):
        lines += ["### Open questions", ""]
        lines += [f"- {question}" for question in plan["open_questions"]]
        lines += [""]

    return "\n".join(lines).strip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    source = Path(args.plan)
    if not source.is_file():
        print("no plan to render")
        return

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(json.loads(source.read_text(encoding="utf-8"))), encoding="utf-8")
    print(f"rendered the plan -> {output}")


if __name__ == "__main__":
    main()
