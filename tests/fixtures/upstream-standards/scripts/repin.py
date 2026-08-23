#!/usr/bin/env python3
"""Re-resolve every upstream this repository inherits, and report which ones moved.

There is deliberately no builtin for this. `lockstep pin` already resolves every ref and rewrites the
lock file; a second implementation of resolution could disagree with the first, and the one that
disagreed would be the one deciding whether to open a pull request. So this reads the lock, runs
`pin`, and reads it again.

The dispatch payload is not consulted. It is data somebody sent, and a payload that could name a ref
would be a payload that could point a consumer at arbitrary code the moment a token leaked. Every
commit here comes from resolving this repository's own `inherits:` against repositories it already
trusts.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def recorded(lock: Path) -> dict[str, str]:
    """alias -> commit, as the lock file currently has it."""
    if not lock.is_file():
        return {}
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {
        alias: str(entry.get("sha") or "")
        for alias, entry in (data.get("inherits") or {}).items()
    }


def moved(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Aliases whose commit changed, plus any that appeared. Sorted, so the output is stable."""
    return sorted(
        alias
        for alias, sha in after.items()
        if sha and before.get(alias) != sha
    )


def emit(name: str, value: str) -> None:
    destination = os.environ.get("GITHUB_OUTPUT")
    if destination:
        with open(destination, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", default=".pipeline/pins.lock")
    parser.add_argument("--pin", default="lockstep pin", help="How to invoke the compiler's pin.")
    args = parser.parse_args()

    lock = Path(args.lock)
    before = recorded(lock)

    result = subprocess.run(args.pin.split(), capture_output=True, text=True, check=False)
    sys.stdout.write(result.stdout)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(result.returncode)

    changed = moved(before, recorded(lock))
    emit("moved", json.dumps(changed))
    if not changed:
        print("every upstream is already at the commit this repository is pinned to")
        return
    for alias in changed:
        print(f"{alias}: moved to {recorded(lock)[alias][:12]}")


if __name__ == "__main__":
    main()
