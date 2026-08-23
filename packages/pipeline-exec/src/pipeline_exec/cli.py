"""Command-line surface.

Every flag here is emitted verbatim by the compiler, so this module is a contract, not just an
interface. `tests/test_contract.py` in the repo root asserts the two stay aligned.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import click

from . import __version__
from .errors import CoverageShortfall, ExecError
from .hygiene import DEFAULT_MAX_FIELD, sanitize
from .items import MATRIX_CAP, Item, as_matrix, as_shards, covered, enforce_cap, load_items, shard_of
from .plugins import register

EXIT_OK = 0
EXIT_FAILED = 1


def _fail(message: str) -> None:
    click.echo(click.style(message, fg="red"), err=True)
    sys.exit(EXIT_FAILED)


def _emit_output(name: str, value: str) -> None:
    """Publish a step output.

    Appends to $GITHUB_OUTPUT when running under Actions and echoes to stdout otherwise, so the
    generated workflow carries no shell redirection and the same command is usable by hand.
    """
    destination = os.environ.get("GITHUB_OUTPUT")
    if destination:
        with open(destination, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    else:
        click.echo(f"{name}={value}")


def _summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="pipeline-exec")
def main() -> None:
    """Deterministic executors for compiled pipeline workflows."""


@main.command(name="list-commands")
@click.option("--extensions-only", is_flag=True, help="List only commands contributed by extensions.")
def list_commands(extensions_only: bool) -> None:
    """List the commands available here, including any an extension contributes.

    `lockstep doctor` cannot verify an extension it does not have installed, so this is how a
    pipeline repository proves in CI that the builtins its spec names actually exist.
    """
    from .plugins import discover

    extensions = set(discover())
    for name in sorted(main.commands):
        if extensions_only and name not in extensions:
            continue
        origin = "extension" if name in extensions else "built-in"
        click.echo(f"{name}\t{origin}")


@main.command()
@click.option("--input", "input_path", required=True, type=click.Path(path_type=Path))
@click.option("--key", "key_field", default="key", show_default=True, help="Item identity field.")
@click.option("--name", default="items", show_default=True, help="Step output name to write.")
@click.option("--only-missing", is_flag=True, help="Drop items whose output already exists.")
@click.option("--output-dir", type=click.Path(path_type=Path), help="Where per-item outputs land.")
@click.option("--output-pattern", default="{key}.json", show_default=True)
@click.option("--max", "max_items", default=MATRIX_CAP, show_default=True, type=int)
@click.option("--shard-threshold", default=0, type=int, help="Above this count, emit shards. 0 = never.")
@click.option("--shards", default=8, show_default=True, type=int, help="Shard count when sharding.")
@click.option("--no-shard", is_flag=True, help="Always emit one leg per item.")
def fanout(
    input_path: Path,
    key_field: str,
    name: str,
    only_missing: bool,
    output_dir: Path | None,
    output_pattern: str,
    max_items: int,
    shard_threshold: int,
    shards: int,
    no_shard: bool,
) -> None:
    """Turn a JSON array into a matrix, as items or as shards.

    The choice depends on how many items there are, which only becomes known here — the compiler
    emits the same matrix expression either way.
    """
    try:
        items = load_items(input_path, key_field)
        if only_missing:
            done = covered(items, output_dir, output_pattern)
            items = [item for item in items if item.key not in done]
            if done:
                click.echo(f"skipping {len(done)} item(s) already covered", err=True)

        if not items:
            _emit_output(name, "[]")
            _emit_output("mode", "items")
            _emit_output("count", "0")
            return

        sharded = not no_shard and shard_threshold > 0 and len(items) > shard_threshold
        if sharded:
            count = max(1, min(shards, len(items)))
            payload: list[dict[str, Any]] = as_shards(count)
            mode = "shards"
            click.echo(f"{len(items)} items above threshold {shard_threshold}: {count} shards", err=True)
        else:
            enforce_cap(len(items), max_items)
            payload = as_matrix(items)
            mode = "items"

        _emit_output(name, json.dumps(payload, separators=(",", ":"), sort_keys=True))
        _emit_output("mode", mode)
        _emit_output("count", str(len(items)))
    except ExecError as error:
        _fail(str(error))


@main.command(name="shard-run")
@click.option("--slice", "slice_json", required=True, help="One matrix value: an item or a shard.")
@click.option("--input", "input_path", required=True, type=click.Path(path_type=Path))
@click.option("--key", "key_field", default="key", show_default=True)
@click.argument("command", nargs=-1, required=True)
def shard_run(slice_json: str, input_path: Path, key_field: str, command: tuple[str, ...]) -> None:
    """Run COMMAND once per item in this matrix leg.

    Accepts either shape the matrix can carry: a single item, or a shard descriptor covering many.
    `{item}` and `{item.field}` in COMMAND are substituted per item. Every item runs even after one
    fails, so a bad item costs its own output and not the whole slice — the leg still exits non-zero.
    """
    try:
        payload = json.loads(slice_json)
    except json.JSONDecodeError as exc:
        _fail(f"--slice is not valid JSON: {exc}")
        return
    if not isinstance(payload, dict):
        _fail("--slice must be a JSON object")
        return

    try:
        if "shard" in payload and "shards" in payload:
            work = shard_of(load_items(input_path, key_field), int(payload["shard"]), int(payload["shards"]))
        else:
            key = str(payload.get(key_field, "item"))
            work = [Item(key=key, value=payload)]
    except ExecError as error:
        _fail(str(error))
        return

    if not work:
        click.echo("no items in this shard", err=True)
        return

    failed: list[str] = []
    for item in work:
        rendered = [_substitute(part, item) for part in command]
        click.echo(f"--- {item.key}: {' '.join(rendered)}", err=True)
        if subprocess.run(rendered, check=False).returncode != 0:
            failed.append(item.key)

    click.echo(f"{len(work) - len(failed)}/{len(work)} succeeded", err=True)
    if failed:
        _fail(f"failed items: {', '.join(failed)}")


def _substitute(text: str, item: Item) -> str:
    if "{item}" in text:
        text = text.replace("{item}", json.dumps(item.value, separators=(",", ":"), sort_keys=True))
    for field, value in item.value.items():
        text = text.replace("{item." + str(field) + "}", str(value))
    return text.replace("{key}", item.key)


@main.command(name="fanout-verify")
@click.option("--dir", "output_dir", required=True, type=click.Path(path_type=Path))
@click.option("--expected", help="JSON array of the items that were fanned out.")
@click.option("--expected-file", type=click.Path(path_type=Path))
@click.option("--key", "key_field", default="key", show_default=True)
@click.option("--output-pattern", default="{key}.json", show_default=True)
@click.option("--min-success-rate", default=1.0, show_default=True, type=float)
def fanout_verify(
    output_dir: Path,
    expected: str | None,
    expected_file: Path | None,
    key_field: str,
    output_pattern: str,
    min_success_rate: float,
) -> None:
    """Decide explicitly what a partially-failed fan-out means.

    Plain `needs:` fails a pipeline on any failed leg; the local runtime saves each item and carries
    on. Neither is right by default, so the policy is stated as a number and checked here.
    """
    try:
        if expected_file:
            items = load_items(expected_file, key_field)
        elif expected:
            items = _items_from_json(expected, key_field)
        else:
            _fail("one of --expected or --expected-file is required")
            return

        if not items:
            click.echo("nothing was fanned out; coverage is vacuously complete")
            return

        present = sorted(covered(items, output_dir, output_pattern))
        missing = sorted({item.key for item in items} - set(present))
        rate = len(present) / len(items)

        report = f"coverage {len(present)}/{len(items)} ({rate:.0%}), required {min_success_rate:.0%}"
        click.echo(report)
        _summary(
            f"### Fan-out coverage\n\n{report}\n" + (f"\nMissing: {', '.join(missing)}\n" if missing else "")
        )
        if missing:
            click.echo(f"missing: {', '.join(missing)}", err=True)
        if rate < min_success_rate:
            raise CoverageShortfall(f"coverage {rate:.0%} is below the required {min_success_rate:.0%}")
    except ExecError as error:
        _fail(str(error))


def _items_from_json(raw: str, key_field: str) -> list[Item]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ExecError(f"--expected is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ExecError("--expected must be a JSON array")
    return [
        Item(key=str(entry.get(key_field, index)) if isinstance(entry, dict) else str(entry), value=entry)
        for index, entry in enumerate(data)
    ]


@main.command(name="validate-schema")
@click.option("--dir", "target_dir", type=click.Path(path_type=Path))
@click.option("--input", "input_path", type=click.Path(path_type=Path))
@click.option("--require", "required", default="", help="Comma-separated keys every object must have.")
@click.option("--max-field-length", default=DEFAULT_MAX_FIELD, show_default=True, type=int)
@click.option("--allow-markup", is_flag=True, help="Keep HTML-ish markup instead of stripping it.")
@click.option("--check", is_flag=True, help="Report problems without rewriting files.")
def validate_schema(
    target_dir: Path | None,
    input_path: Path | None,
    required: str,
    max_field_length: int,
    allow_markup: bool,
    check: bool,
) -> None:
    """Validate and sanitize agent output before anything downstream consumes it.

    Runs at both boundaries: inside the agent's own workflow, so a poisoned leg fails at source, and
    again where the orchestrator collects results.
    """
    paths = _targets(target_dir, input_path)
    if paths is None:
        return
    keys = [key.strip() for key in required.split(",") if key.strip()]

    problems: list[str] = []
    cleaned = 0
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{path}: not valid JSON ({exc})")
            continue

        missing = [key for key in keys if not (isinstance(data, dict) and key in data)]
        if missing:
            problems.append(f"{path}: missing required key(s) {', '.join(missing)}")
            continue

        sanitized = sanitize(data, max_field=max_field_length, strip_markup=not allow_markup)
        if sanitized != data:
            cleaned += 1
            if not check:
                path.write_text(json.dumps(sanitized, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    click.echo(f"validated {len(paths)} file(s); {cleaned} sanitized")
    if problems:
        for problem in problems:
            click.echo(problem, err=True)
        _fail(f"{len(problems)} file(s) failed validation")


def _targets(target_dir: Path | None, input_path: Path | None) -> list[Path] | None:
    if input_path:
        if not input_path.is_file():
            _fail(f"{input_path} does not exist")
            return None
        return [input_path]
    if target_dir:
        if not target_dir.is_dir():
            # An absent directory means the producing step ran and wrote nothing, or never ran at
            # all. Either way there is nothing to validate, and failing here would mask the cause.
            click.echo(f"{target_dir} does not exist; nothing to validate")
            return []
        return sorted(target_dir.rglob("*.json"))
    _fail("one of --dir or --input is required")
    return None


@main.command(name="wait-for")
@click.option("--url", "urls", multiple=True, required=True, help="Repeatable; all must respond.")
@click.option("--timeout", default=120, show_default=True, type=int, help="Seconds.")
@click.option("--interval", default=3, show_default=True, type=int, help="Seconds between attempts.")
def wait_for(urls: tuple[str, ...], timeout: int, interval: int) -> None:
    """Block until every URL responds, so tests do not race a still-booting application.

    Without this, browser sessions hit a half-started app, the failures look like product bugs, and
    the repair loop spends credits diagnosing a startup race.
    """
    import time
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    pending = list(urls)
    while pending and time.monotonic() < deadline:
        for url in list(pending):
            try:
                with urllib.request.urlopen(url, timeout=interval):  # noqa: S310
                    click.echo(f"ready: {url}")
                    pending.remove(url)
            except (urllib.error.URLError, OSError, ValueError):
                pass
        if pending:
            time.sleep(interval)
    if pending:
        _fail(f"timed out after {timeout}s waiting for: {', '.join(pending)}")


# --- extracted executors ---------------------------------------------------
#
# These wrap the code moved over from pipeline-framework. The executors carry resilience behaviour
# that was earned against a real application — 409/422 recovery, PATCH/PUT fallback, retry ladders,
# browser auto-login and crash recovery, runtime variable tracking — so the modules were copied
# verbatim and only their imports and configuration plumbing were adapted.


def _config(**overrides: object) -> Any:
    from .config import ExecConfig

    return ExecConfig.from_env(**overrides)


@main.command(name="test-runner")
@click.option("--scripts-dir", default="", help="Where the committed test scripts live.")
@click.option("--tags-file", default="", help="Tag toggles (.env-tests).")
@click.option("--run-dir", default="outputs/runs/current", show_default=True)
@click.option("--output-dir", default="", help="Pipeline output directory.")
@click.option("--parallel", default=0, type=int, help="Concurrent scripts; 0 uses the default.")
@click.option("--changed", is_flag=True, help="Only scripts modified since their last execution.")
@click.option("--story", default="", help="Comma-separated story ids to run.")
def test_runner(
    scripts_dir: str,
    tags_file: str,
    run_dir: str,
    output_dir: str,
    parallel: int,
    changed: bool,
    story: str,
) -> None:
    """Execute committed JSON test scripts against the target application."""
    import asyncio

    from .builtins.test_runner import run_test_pipeline

    config = _config(scripts_dir=scripts_dir, tags_file=tags_file, output_dir=output_dir)
    summary = asyncio.run(
        run_test_pipeline(
            config,
            run_dir=run_dir,
            changed_only=changed,
            story_filter=story,
            concurrency=parallel,
        )
    )
    click.echo(json.dumps(summary))
    _summary(
        f"### Tests\n\n{summary['passed']} passed, {summary['failed']} failed, "
        f"{summary['skipped']} skipped of {summary['total']}\n"
    )
    if summary.get("failed"):
        sys.exit(EXIT_FAILED)


@main.command()
@click.option("--output", required=True, type=click.Path(path_type=Path))
@click.option("--output-dir", default="", help="Pipeline output directory.")
def discover(output: Path, output_dir: str) -> None:
    """Discover the target application's API surface."""
    import asyncio

    from .builtins.discovery import discover_api

    schemas = asyncio.run(discover_api(_config(output_dir=output_dir)))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(schemas, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    click.echo(f"discovered {len(schemas)} schema group(s) -> {output}")


@main.command()
@click.option("--run-dir", default="outputs/runs/current", show_default=True, type=click.Path(path_type=Path))
@click.option("--output-dir", default="outputs", show_default=True)
@click.option("--index/--no-index", default=True, help="Also refresh the run index page.")
def report(run_dir: Path, output_dir: str, index: bool) -> None:
    """Render the HTML dashboard for a run."""
    from .reports.collect import build_dashboard_data
    from .reports.dashboard import generate_dashboard, generate_index_page

    if not run_dir.is_dir():
        click.echo(f"{run_dir} does not exist; nothing to report")
        return
    config = _config(output_dir=output_dir)
    data = build_dashboard_data(run_dir, output_dir)
    path = generate_dashboard(str(run_dir), data, config)
    click.echo(f"dashboard -> {path}")
    if index:
        click.echo(f"index -> {generate_index_page(output_dir, config)}")


@main.command(name="collect-failures")
@click.option("--run-dir", default="outputs/runs/current", show_default=True, type=click.Path(path_type=Path))
@click.option("--output", required=True, type=click.Path(path_type=Path))
@click.option("--scripts-dir", default="test-scripts", show_default=True, type=click.Path(path_type=Path))
@click.option("--report-chars", default=4000, show_default=True, type=int)
@click.option("--script-chars", default=3000, show_default=True, type=int)
def collect_failures(
    run_dir: Path, output: Path, scripts_dir: Path, report_chars: int, script_chars: int
) -> None:
    """Gather failed execution reports into one file for an agent to analyze."""
    import re

    output.parent.mkdir(parents=True, exist_ok=True)
    exec_dir = run_dir / "executions"
    if not exec_dir.is_dir():
        output.write_text("[]\n", encoding="utf-8")
        click.echo("no executions found; nothing to collect")
        return

    failures = []
    examined = 0
    for report_file in sorted(exec_dir.glob("*.md")):
        content = report_file.read_text(encoding="utf-8")
        examined += 1
        if not re.search(r"\*\*FAILED\*\*", content, re.IGNORECASE):
            continue
        script_path = scripts_dir / f"{report_file.stem}.json"
        failures.append(
            {
                "key": report_file.stem,
                "story_id": report_file.stem,
                "report": content[:report_chars],
                "test_script": script_path.read_text(encoding="utf-8")[:script_chars]
                if script_path.is_file()
                else "",
            }
        )

    output.write_text(json.dumps(failures, indent=2) + "\n", encoding="utf-8")
    click.echo(f"collected {len(failures)} failure(s) from {examined} report(s) -> {output}")


@main.command(name="check-convergence")
@click.option("--run-dir", default="outputs/runs/current", show_default=True, type=click.Path(path_type=Path))
@click.option("--name", default="converged", show_default=True, help="Step output name to write.")
def check_convergence(run_dir: Path, name: str) -> None:
    """Report whether the repair loop has converged.

    Always exits 0: convergence is a result, not a failure, and a non-zero exit would fail the job
    that is meant to read the answer. The verdict goes to the step output; the detail goes to stderr.
    """
    from collections import Counter

    classifications_path = run_dir / "heal-classifications.json"
    counts: Counter[str] = Counter()
    if classifications_path.is_file():
        loaded = json.loads(classifications_path.read_text(encoding="utf-8"))
        counts = Counter(
            entry.get("category", "unknown") for entry in loaded.values() if isinstance(entry, dict)
        )

    script_bugs = counts.get("script_bug", 0)
    converged = script_bugs == 0
    _emit_output(name, "true" if converged else "false")
    click.echo(
        json.dumps(
            {
                "converged": converged,
                "script_bugs": script_bugs,
                "app_bugs": counts.get("app_bug", 0),
                "infra": counts.get("infra_issue", 0),
            }
        ),
        err=True,
    )


def _gh_json(*args: str) -> Any:
    """Call `gh api` and parse JSON. Separated so tests can supply fixtures instead."""
    result = subprocess.run(["gh", "api", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        _fail(f"gh api {' '.join(args)} failed: {result.stderr.strip()[:200]}")
    return json.loads(result.stdout or "[]")


@main.command(name="pr-feedback")
@click.option("--pr", default="", help="Pull request number. Empty before one exists.")
@click.option("--repo", envvar="GITHUB_REPOSITORY", default="")
@click.option("--output", required=True, type=click.Path(path_type=Path))
@click.option("--from-dir", type=click.Path(path_type=Path), help="Read fixtures instead of the API.")
@click.option("--by-path", is_flag=True, help="Also group inline comments by the file they concern.")
def pr_feedback(pr: str, repo: str, output: Path, from_dir: Path | None, by_path: bool) -> None:
    """Collect a pull request's review feedback as pipeline input.

    A first run has no pull request and therefore no feedback. That is the normal case, not an
    error: the same step runs on every invocation and simply has nothing to say the first time.
    """
    from .feedback import group_by_path, normalize

    if from_dir:

        def load(name: str) -> list[dict[str, Any]]:
            path = from_dir / f"{name}.json"
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []

        reviews, comments, discussion = load("reviews"), load("comments"), load("issue_comments")
    elif not pr.strip():
        reviews, comments, discussion = [], [], []
        click.echo("no pull request yet; no feedback to collect")
    else:
        if not repo:
            _fail("--repo is required (or set GITHUB_REPOSITORY)")
        reviews = _gh_json(f"repos/{repo}/pulls/{pr}/reviews", "--paginate")
        comments = _gh_json(f"repos/{repo}/pulls/{pr}/comments", "--paginate")
        discussion = _gh_json(f"repos/{repo}/issues/{pr}/comments", "--paginate")

    feedback = normalize(reviews, comments, discussion)
    if by_path:
        feedback["by_path"] = group_by_path(feedback)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(feedback, indent=2) + "\n", encoding="utf-8")

    _emit_output("count", str(feedback["count"]))
    _emit_output("requested_changes", "true" if feedback["requested_changes"] else "false")
    click.echo(f"collected {feedback['count']} piece(s) of feedback -> {output}")


@main.command(name="parse-command")
@click.option("--command", required=True, help="The slash command to look for, e.g. /implement.")
@click.option("--body", default="", help="The comment body.")
@click.option("--body-file", type=click.Path(path_type=Path), help="Read the body from a file.")
@click.option("--names", default="", help="Comma-separated argument names for bare positionals.")
def parse_command(command: str, body: str, body_file: Path | None, names: str) -> None:
    """Read a chat-ops command out of a comment.

    Publishes `matched`, every named argument, and `instruction` — the free text the human wrote
    after the command, which is usually the most useful thing in the comment.
    """
    from .command import parse

    text = body_file.read_text(encoding="utf-8") if body_file else body
    declared = [name.strip() for name in names.split(",") if name.strip()]
    invocation = parse(text, command, names=declared)

    _emit_output("matched", "true" if invocation.matched else "false")
    if not invocation.matched:
        click.echo(f"no {command} in this comment", err=True)
        return

    for key, value in sorted(invocation.arguments.items()):
        _emit_output(key, value)
    _emit_output("instruction", invocation.instruction.replace("\n", " ")[:2000])
    _emit_output("arguments", json.dumps(invocation.arguments, sort_keys=True))
    click.echo(f"{invocation.command} {invocation.arguments}", err=True)


@main.command(name="cache-key")
@click.option("--prefix", required=True, help="Stable prefix identifying the pipeline and step.")
@click.option("--inputs", "inputs_text", default="", help="Newline-separated files to hash.")
@click.option("--extra", default="", help="Runtime values that change behaviour without changing files.")
@click.option("--name", default="key", show_default=True, help="Step output name to write.")
def cache_key(prefix: str, inputs_text: str, extra: str, name: str) -> None:
    """Compute a step's content-addressed cache key.

    The key covers the declared inputs' contents, not their timestamps, so a step re-runs when its
    script, its definition, or an upstream output it reads actually changes. A missing input is
    hashed as missing rather than raising: an upstream output that was never produced must not
    resolve to the same key as one that was.
    """
    import hashlib

    digest = hashlib.sha256()
    for path_text in sorted({line.strip() for line in inputs_text.splitlines() if line.strip()}):
        path = Path(path_text)
        digest.update(path_text.encode("utf-8"))
        if path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        elif path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file():
                    digest.update(str(child).encode("utf-8"))
                    digest.update(hashlib.sha256(child.read_bytes()).digest())
        else:
            digest.update(b"\x00missing")
    digest.update(extra.encode("utf-8"))
    _emit_output(name, f"{prefix}-{digest.hexdigest()[:16]}")


# Extensions load last, so a built-in command can never be shadowed by one.
_registered = register(main)
