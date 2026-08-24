"""Command-line surface.

Every flag here is emitted verbatim by the compiler, so this module is a contract, not just an
interface. `tests/test_contract.py` in the repo root asserts the two stay aligned.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import click

from . import __version__
from .errors import CoverageShortfall, ExecError
from .hygiene import DEFAULT_MAX_FIELD, sanitize
from .items import MATRIX_CAP, Item, as_matrix, as_shards, covered, enforce_cap, load_items, shard_of
from .plugins import register

EXIT_OK = 0
EXIT_FAILED = 1


def _fail(message: str) -> NoReturn:
    """Say why, and stop. `NoReturn` because it does not return, and saying so lets a type checker
    see that everything after a `_fail` is a branch that cannot be reached."""
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
@click.option(
    "--surface",
    required=True,
    type=click.Path(path_type=Path),
    help="Declared API surface: `openapi:` and/or `paths:`. Lives with the pipeline's contexts.",
)
@click.option("--output", required=True, type=click.Path(path_type=Path), help="Where to write the JSON.")
@click.option(
    "--context",
    type=click.Path(path_type=Path),
    help="Also write what was found as a context fragment an agent can import.",
)
@click.option("--token", default="", help="Bearer token, when the target needs one.")
@click.option("--insecure", is_flag=True, help="Skip TLS verification. For a self-signed test target only.")
@click.option("--output-dir", default="", help="Pipeline output directory.")
def discover(
    surface: Path, output: Path, context: Path | None, token: str, insecure: bool, output_dir: str
) -> None:
    """Record the target's API surface, as declared by this pipeline.

    The surface is declared rather than guessed: a framework that ships a list of endpoints ships one
    application's endpoints. See docs/layers.md.
    """
    import asyncio

    from .builtins.discovery import Surface, discover_api, write_context

    result = asyncio.run(
        discover_api(
            _config(output_dir=output_dir),
            Surface.load(surface),
            token=token,
            verify_tls=not insecure,
        )
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if context:
        write_context(result, context)
    click.echo(f"recorded the surface of {result['api_url']} -> {output}")


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


@main.command(name="gh-issue-fetch")
@click.option("--issue", required=True, help="Issue number, `#123`, or the issue's URL.")
@click.option("--repo", envvar="GITHUB_REPOSITORY", default="")
@click.option("--output", required=True, type=click.Path(path_type=Path))
@click.option("--no-discussion", is_flag=True, help="Skip the comment thread.")
@click.option("--from-dir", type=click.Path(path_type=Path), help="Read fixtures instead of the API.")
def gh_issue_fetch(issue: str, repo: str, output: Path, no_discussion: bool, from_dir: Path | None) -> None:
    """Fetch one GitHub issue and reduce it to what an implementing agent needs.

    The counterpart to a tracker-specific fetcher, and simpler than one: acceptance criteria are a
    task list or a heading, both of which GitHub renders itself, so nothing here has to guess which
    custom field somebody put them in.
    """
    from .issues import reduce_issue

    number = _issue_number(issue)
    if from_dir:

        def load(name: str, default: Any) -> Any:
            path = from_dir / f"{name}.json"
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default

        raw, comments = load("issue", {}), load("comments", [])
        repo = repo or str(raw.get("repository", ""))
    else:
        if not repo:
            _fail("--repo is required (or set GITHUB_REPOSITORY)")
        raw = _gh_json(f"repos/{repo}/issues/{number}")
        if raw.get("pull_request"):
            # The issues endpoint answers for pull requests too, and returns something that looks
            # like an issue. Implementing a pull request as if it were an issue is not a thing.
            _fail(f"{repo}#{number} is a pull request, not an issue")
        comments = [] if no_discussion else _gh_json(f"repos/{repo}/issues/{number}/comments", "--paginate")

    document = reduce_issue(raw, [] if no_discussion else comments, repo=repo)
    if not document["number"]:
        _fail(f"no issue found for {issue!r}")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    _emit_output("number", str(document["number"]))
    _emit_output("state", document["state"])
    # Nothing outstanding: on GitHub an agent's conclusions reach the issue through safe outputs.
    _emit_output("writeback", json.dumps([]))
    _emit_output("criteria", str(len(document["acceptance_criteria"])))
    click.echo(
        f"fetched {document['key']} \u2014 {len(document['acceptance_criteria'])} criteria, "
        f"{len(document['discussion'])} comment(s) -> {output}"
    )


def _issue_number(issue: str) -> str:
    """`123`, `#123`, `owner/repo#123` and a browser URL all name the same issue."""
    text = issue.strip().rstrip("/")
    tail = text.rsplit("/", 1)[-1].rsplit("#", 1)[-1]
    if not tail.isdigit():
        _fail(f"cannot read an issue number out of {issue!r}")
    return tail


@main.command(name="scan-input")
@click.option("--input", "inputs", multiple=True, required=True, type=click.Path(path_type=Path))
@click.option("--mode", type=click.Choice(["warn", "block"]), default="warn", show_default=True)
@click.option(
    "--fail-on",
    type=click.Choice(["critical", "high", "medium"]),
    default="critical",
    show_default=True,
    help="Lowest severity that fails the step in block mode.",
)
@click.option("--report", type=click.Path(path_type=Path), help="Where to write the findings.")
def scan_input(inputs: tuple[Path, ...], mode: str, fail_on: str, report: Path | None) -> None:
    """Look for instructions hidden in the data an agent is about to read.

    The shipped baseline guardrail asks a model to treat its input as data. This is the half that
    does not depend on the model agreeing: it runs first, on the files the agent will be handed.

    It is not a filter that makes untrusted input safe — pattern matching cannot decide what a
    sentence means. What bounds a successful injection is the read-only permissions, the tool
    deny-list and the egress rules the same guardrail compiles into the workflow. This narrows the
    gap between telling the model and checking, and says what it found.
    """
    from .injection import scan

    ranks = {"medium": 0, "high": 1, "critical": 2}
    scanned: list[str] = []
    findings: list[dict[str, Any]] = []
    for path in inputs:
        files = sorted(path.rglob("*")) if path.is_dir() else [path]
        for target in files:
            if not target.is_file():
                continue
            scanned.append(str(target))
            for finding in scan(target.read_text(encoding="utf-8", errors="replace")):
                findings.append({**finding.as_dict(), "file": str(target)})

    if not scanned:
        # An agent about to read nothing is a pipeline bug, and reporting a clean scan of no files
        # is the kind of green that hides one.
        _fail(f"nothing to scan at {', '.join(str(p) for p in inputs)}")

    summary: dict[str, Any] = {
        "total": len(findings),
        "files_scanned": len(scanned),
        "by_severity": {level: sum(1 for f in findings if f["severity"] == level) for level in ranks},
        "categories": sorted({str(f["category"]) for f in findings}),
        "findings": findings,
    }

    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    for hit in findings:
        click.echo(
            f"  {hit['severity']:8} {hit['category']:22} {hit['file']}:{hit['line']}  {hit['excerpt']}"
        )

    blocking = [hit for hit in findings if ranks[str(hit["severity"])] >= ranks[fail_on]]
    _emit_output("findings", str(len(findings)))
    _emit_output("blocking", str(len(blocking)))
    click.echo(
        f"scanned {len(scanned)} file(s): {len(findings)} finding(s), {len(blocking)} at or above {fail_on}"
    )
    if blocking and mode == "block":
        _fail(
            f"{len(blocking)} finding(s) at or above {fail_on} in input an agent was about to read. "
            "Relax `enforce.scan-input` in the guardrail that sets it, or look at what was found"
        )


@main.command(name="eval-cases")
@click.option(
    "--cases",
    required=True,
    multiple=True,
    type=click.Path(path_type=Path),
    help="Directory of case files. Repeatable: an inherited suite plus your own.",
)
@click.option("--output-dir", required=True, type=click.Path(path_type=Path))
@click.option(
    "--repo-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Where to lay down each case's fixture tree, one directory per case.",
)
def eval_cases(cases: tuple[Path, ...], output_dir: Path, repo_dir: Path | None) -> None:
    """Write one agent input per eval case, and list them for a fan-out.

    An eval is not a special way of running an agent. It is the ordinary way — `input_path` in,
    `output_path` out — with the input coming from a case file instead of an earlier step.

    A case naming a fixture also gets that tree copied under `--repo-dir`, and the path written into
    the input it is handed. An agent asked to review code needs code to read.
    """
    from .evals import Case, CaseError, expand, gather

    try:
        files = gather(list(cases))
        if not files:
            _fail("no cases in " + ", ".join(str(c) for c in cases))
        parsed = [Case.load(path) for path in files]
        items = expand(parsed, output_dir, repos=repo_dir)
    except CaseError as error:
        _fail(str(error))

    fixtures = sum(1 for item in items if item["fixture"])
    _emit_output("cases", json.dumps([item["case"] for item in items]))
    _emit_output("count", str(len(items)))
    click.echo(
        f"{len(items)} case(s) -> {output_dir}"
        + (f", {fixtures} with a fixture -> {repo_dir}" if fixtures else "")
    )


@main.command(name="eval-judge-prep")
@click.option("--cases", required=True, multiple=True, type=click.Path(path_type=Path))
@click.option("--outputs", required=True, type=click.Path(path_type=Path), help="What the agent answered.")
@click.option("--output-dir", required=True, type=click.Path(path_type=Path))
def eval_judge_prep(cases: tuple[Path, ...], outputs: Path, output_dir: Path) -> None:
    """Pair each rubric with the answer it is about, for a judging agent to read.

    Only cases carrying a rubric that also produced an answer. A case with no answer has already
    failed for that reason; sending it to a judge would spend a model call to be told so again.
    """
    from .evals import Case, CaseError, gather, judge_inputs

    try:
        parsed = [Case.load(path) for path in gather(list(cases))]
    except CaseError as error:
        _fail(str(error))

    pending = judge_inputs(parsed, outputs, output_dir)
    _emit_output("pending", json.dumps(pending))
    _emit_output("count", str(len(pending)))
    click.echo(f"{len(pending)} rubric(s) to judge -> {output_dir}")


@main.command(name="eval-grade")
@click.option(
    "--cases",
    required=True,
    multiple=True,
    type=click.Path(path_type=Path),
    help="Directory of case files. Repeatable: an inherited suite plus your own.",
)
@click.option("--outputs", required=True, type=click.Path(path_type=Path), help="Directory of agent outputs.")
@click.option("--output", required=True, type=click.Path(path_type=Path), help="Where to write the report.")
@click.option("--agent", default="", help="Name recorded in the report.")
@click.option("--min-pass-rate", type=float, default=None, help="Fail below this rate of decided cases.")
@click.option(
    "--min-score",
    type=float,
    default=None,
    help="Fail below this mean score across the scored cases a judge decided.",
)
@click.option(
    "--judgements",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory of judge verdicts, one per case. Without it, rubrics stay undecided.",
)
@click.option(
    "--history-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Record this run in the ledger, so a later change can be compared against it.",
)
@click.option(
    "--prompt-file",
    type=click.Path(path_type=Path),
    default=None,
    help="The compiled agent workflow, hashed to fingerprint the prompt this run scored.",
)
def eval_grade(
    cases: tuple[Path, ...],
    outputs: Path,
    output: Path,
    agent: str,
    min_pass_rate: float | None,
    min_score: float | None,
    judgements: Path | None,
    history_dir: Path | None,
    prompt_file: Path | None,
) -> None:
    """Grade an agent's answers against its eval cases.

    Applies the deterministic half of each case — required fields, text that must and must not
    appear, counts. A case carrying a rubric is reported as awaiting judgement rather than scored
    here: this command has no model, and grading prose without one would invent the number.

    An output file missing for a case is a failure, not a skip. The agent was asked and did not
    answer, which is exactly the regression an eval suite is for.
    """
    from .evals import Case, CaseError, apply_judgement, gather, grade, summarize, unanswered

    try:
        files = gather(list(cases))
    except CaseError as error:
        _fail(str(error))
    if not files:
        _fail("no cases in " + ", ".join(str(c) for c in cases))

    results: list[dict[str, Any]] = []
    for path in files:
        try:
            case = Case.load(path)
        except CaseError as error:
            _fail(str(error))
        answer_path = outputs / f"{case.name}.json"
        if not answer_path.is_file():
            results.append(unanswered(case))
            continue
        try:
            answer = json.loads(answer_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            _fail(f"{case.name}: the agent's output is not valid JSON ({error.msg})")
        result = grade(case, answer)
        verdict_path = (judgements / f"{case.name}.json") if judgements else None
        if result["rubric_pending"] and verdict_path and verdict_path.is_file():
            try:
                result = apply_judgement(result, json.loads(verdict_path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                result = apply_judgement(result, None)
        results.append(result)

    summary = summarize(results, min_pass_rate=min_pass_rate, min_score=min_score)
    report = {"agent": agent, "summary": summary, "cases": results}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    if history_dir:
        _record_eval(report, history_dir, prompt_file)

    for result in results:
        if not result["deterministic_passed"]:
            for check in result["checks"]:
                if not check["passed"]:
                    click.echo(
                        f"  {result['case']}: {check['check']} {check['target']!r} — {check['detail']}"
                    )

    _emit_output("passed", "true" if summary["ok"] else "false")
    _emit_output("pass_rate", str(summary["pass_rate"]))
    _emit_output("pending_rubric", str(len(summary["pending_rubric"])))
    _emit_output("mean_score", "" if summary["mean_score"] is None else str(summary["mean_score"]))
    click.echo(
        f"{summary['passed']}/{summary['total'] - len(summary['pending_rubric'])} decided case(s) passed"
        + (f", {len(summary['pending_rubric'])} awaiting judgement" if summary["pending_rubric"] else "")
        + (f", mean score {summary['mean_score']}" if summary["mean_score"] is not None else "")
        + f" -> {output}"
    )
    # A floor on a suite where nothing was scored is a gate with nothing behind it. Say so rather
    # than reporting a pass that the floor had no part in.
    if min_score is not None and summary["mean_score"] is None:
        click.echo(f"note: --min-score={min_score} decided nothing — no scored rubric was judged")
    if not summary["ok"]:
        sys.exit(1)


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
    # Bare words, as a list. `/review security intent` asks for two things, and how many is not
    # known until somebody types it — so they cannot be declared as named arguments.
    _emit_output("positional", json.dumps(invocation.positional))
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


@main.command(name="meter")
@click.option(
    "--usage",
    required=True,
    type=click.Path(path_type=Path),
    help="Directory of gh-aw usage artifacts downloaded for this run.",
)
@click.option("--output", required=True, type=click.Path(path_type=Path), help="Where to write OTLP.")
@click.option("--pricing", type=click.Path(path_type=Path), help="JSON map of model to dollars per credit.")
@click.option("--endpoint", default="", help="OTLP/HTTP collector base URL. Metrics POST to /v1/metrics.")
@click.option("--service-name", default="lockstep", help="service.name on the exported resource.")
@click.option("--title", default="Run cost", help="Heading for the job summary table.")
@click.option(
    "--jobs",
    type=click.Path(path_type=Path),
    default=None,
    help="A saved /actions/runs/{id}/jobs response: outcomes, durations and queue times.",
)
@click.option(
    "--history-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Write the run's durable record under here, sharded by month.",
)
@click.option("--explain", is_flag=True, help="Print every file read and every number matched.")
@click.option(
    "--require-usage",
    is_flag=True,
    help="Fail when no usage record was found, rather than reporting that none was.",
)
def meter(
    usage: Path,
    output: Path,
    pricing: Path | None,
    endpoint: str,
    service_name: str,
    title: str,
    jobs: Path | None,
    history_dir: Path | None,
    explain: bool,
    require_usage: bool,
) -> None:
    """Turn a run's measured credits into dollars, OTLP metrics, and a line in the log.

    Credits come from gh-aw, which measured them. Dollars come from a rate table somebody wrote, so
    they are derived rather than observed, and a model the table does not name is reported as
    unpriced rather than as free. A cost report that says $0.00 because it did not recognise a model
    is the one number worse than no report at all.
    """
    import time

    from .otel import (
        history_file,
        history_line,
        metrics_document,
        price,
        read_jobs,
        read_usage,
        render_summary,
        run_record,
        run_shape,
    )

    rates: dict[str, float] = {}
    if pricing and pricing.is_file():
        try:
            raw = json.loads(pricing.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            _fail(f"pricing table is not valid JSON ({error.msg})")
        rates = {str(k): float(v) for k, v in (raw or {}).items()}

    records, rollups = read_usage(usage) if usage.is_dir() else ([], [])
    priced = price(records, rates, rollups)
    summary = priced.summary()
    run_jobs = read_jobs(jobs) if jobs else []
    attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", "1") or 1)

    if explain:
        click.echo(f"read {usage}")
        for record in records:
            click.echo(f"  measurement {record.source}: {record.credits:g} credits, model={record.model!r}")
        for record in rollups:
            click.echo(f"  roll-up (not summed) {record.source}: {record.credits:g} credits")

    resource = {
        "service.name": service_name,
        "vcs.repository.name": os.environ.get("GITHUB_REPOSITORY", ""),
        "cicd.pipeline.name": os.environ.get("GITHUB_WORKFLOW", ""),
        "cicd.pipeline.run.id": os.environ.get("GITHUB_RUN_ID", ""),
        "vcs.ref.head.name": os.environ.get("GITHUB_REF_NAME", ""),
        "vcs.ref.head.revision": os.environ.get("GITHUB_SHA", ""),
    }
    document = metrics_document(
        priced, resource=resource, nanos=time.time_ns(), jobs=run_jobs, attempt=attempt
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    _summary(render_summary(priced, title=title, jobs=run_jobs))
    _emit_output("credits", str(summary["credits"]))
    _emit_output("dollars", str(summary["dollars"]))
    _emit_output("priced_fraction", str(summary["priced_fraction"]))
    _emit_output("records", str(summary["records"]))
    if run_jobs:
        shape = run_shape(run_jobs)
        _emit_output("wall_seconds", str(shape["wall_seconds"]))
        _emit_output("busy_seconds", str(shape["busy_seconds"]))
        _emit_output("failed_jobs", str(len(shape["failed"])))

    if history_dir:
        finished = datetime.now(UTC).isoformat(timespec="seconds")
        entry = run_record(
            priced,
            run_jobs,
            attempt=attempt,
            identity={
                "run_id": os.environ.get("GITHUB_RUN_ID", ""),
                "run_url": (
                    f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                    f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/"
                    f"{os.environ.get('GITHUB_RUN_ID', '')}"
                ),
                "workflow": os.environ.get("GITHUB_WORKFLOW", ""),
                "event": os.environ.get("GITHUB_EVENT_NAME", ""),
                "ref": os.environ.get("GITHUB_REF_NAME", ""),
                "sha": os.environ.get("GITHUB_SHA", ""),
                "finished": finished,
            },
        )
        target = history_dir / Path(history_file(finished)).name
        target.parent.mkdir(parents=True, exist_ok=True)
        # Appended, not written: the publishing step re-clones the branch and appends again on a
        # conflict, and appends commute. A rewrite would lose whichever run pushed first.
        with target.open("a", encoding="utf-8") as handle:
            handle.write(history_line(entry))
        click.echo(f"recorded run {entry['run_id'] or '(local)'} -> {target}")

    if endpoint:
        _post_metrics(endpoint, document)

    click.echo(
        f"{summary['credits']:g} credits, ${summary['dollars']:,.4f}"
        + (f" ({summary['priced_fraction']:.0%} priced)" if summary["unpriced_models"] else "")
        + f" -> {output}"
    )
    if summary["unpriced_models"]:
        click.echo("unpriced: " + ", ".join(summary["unpriced_models"]))
    if not records:
        message = (
            f"no usage records under {usage} — reporting nothing found rather than a cost of zero. "
            "Run with --explain to see what was read"
        )
        if require_usage:
            _fail(message)
        click.echo(message)


def _post_metrics(endpoint: str, document: dict[str, Any]) -> None:
    """Send one OTLP/HTTP metrics document to a collector.

    A failed export does not fail the pipeline. The run's work is already done and its cost is
    already written to the artifact and the log; losing a metric is worth a loud line, not a red
    build somebody has to re-run.
    """
    import urllib.error
    import urllib.request

    url = endpoint.rstrip("/")
    if not url.endswith("/v1/metrics"):
        url = f"{url}/v1/metrics"
    request = urllib.request.Request(
        url,
        data=json.dumps(document).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            click.echo(f"exported to {url} ({response.status})")
    except (urllib.error.URLError, OSError) as error:
        click.echo(click.style(f"OTLP export to {url} failed: {error}", fg="yellow"), err=True)


@main.command(name="issue-fetch")
@click.option("--source", default="github", type=click.Choice(["github", "jira"]), show_default=True)
@click.option("--issue", required=True, help="Issue number or `#123` for GitHub; the key for Jira.")
@click.option("--output", required=True, type=click.Path(path_type=Path))
@click.option("--repo", envvar="GITHUB_REPOSITORY", default="", help="GitHub only.")
@click.option("--base-url", envvar="JIRA_BASE_URL", default="", help="Jira only.")
@click.option("--criteria-field", envvar="JIRA_CRITERIA_FIELD", default="", help="Jira only.")
@click.option("--no-discussion", is_flag=True, help="Skip the comment thread.")
@click.option("--from-dir", type=click.Path(path_type=Path), help="Read fixtures instead of an API.")
def issue_fetch(
    source: str,
    issue: str,
    output: Path,
    repo: str,
    base_url: str,
    criteria_field: str,
    no_discussion: bool,
    from_dir: Path | None,
) -> None:
    """Fetch one issue from whichever tracker this repository uses, in one shape.

    A pipeline that reads `summary`, `description` and `acceptance_criteria` should not have to know
    which tracker delivered them, and an agent's eval cases should be about the work rather than
    about an API. What genuinely differs stays alongside rather than being flattened: a Jira issue
    type is a real thing and is not a GitHub label.
    """
    if source == "github":
        ctx = click.get_current_context()
        ctx.invoke(
            gh_issue_fetch,
            issue=issue,
            repo=repo,
            output=output,
            no_discussion=no_discussion,
            from_dir=from_dir,
        )
        return

    from .issues import reduce_jira_issue

    if from_dir:
        path = from_dir / "issue.json"
        raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    else:
        token = os.environ.get("JIRA_API_TOKEN", "")
        if not base_url or not token:
            _fail("JIRA_BASE_URL and JIRA_API_TOKEN are required unless --from-dir is given")
        raw = _jira_json(base_url, token, issue)

    document = reduce_jira_issue(raw, criteria_field=criteria_field)
    if not document["key"]:
        _fail(f"no issue found for {issue!r}")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    _emit_output("key", document["key"])
    _emit_output("state", document["state"])
    # What this fetch left for a later step to write back. A list, because the step conditions that
    # read it are membership tests — and because "the write-backs still outstanding" is the honest
    # reading: on GitHub the agent's safe outputs do it and nothing is outstanding.
    _emit_output("writeback", json.dumps(["jira"]))
    _emit_output("criteria", str(len(document["acceptance_criteria"])))
    # Published because "none found" and "guessed from a field nobody configured" are different
    # situations, and a step that reported only a count would make them look identical.
    _emit_output("criteria_source", document["criteria_source"])
    click.echo(
        f"fetched {document['key']} — {len(document['acceptance_criteria'])} criteria "
        f"({document['criteria_source']}) -> {output}"
    )


def _jira_json(base_url: str, token: str, key: str) -> dict[str, Any]:
    import urllib.error
    import urllib.parse
    import urllib.request

    url = f"{base_url.rstrip('/')}/rest/api/2/issue/{urllib.parse.quote(key)}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return dict(json.loads(response.read().decode("utf-8")))
    except urllib.error.HTTPError as error:
        _fail(f"issue tracker returned {error.code} for {key}")
    except (urllib.error.URLError, OSError) as error:
        _fail(f"could not reach {base_url}: {error}")
    return {}


@main.command(name="pr-diff")
@click.option("--pr", required=True, help="Pull request number.")
@click.option("--repo", envvar="GITHUB_REPOSITORY", default="")
@click.option("--output", required=True, type=click.Path(path_type=Path))
@click.option("--from-dir", type=click.Path(path_type=Path), help="Read fixtures instead of the API.")
def pr_diff(pr: str, repo: str, output: Path, from_dir: Path | None) -> None:
    """Fetch a pull request's diff and metadata, reduced to what a review can act on.

    Most of the work is deciding what *not* to send. A reviewer handed 40,000 lines produces a review
    of the first 2,000 and a confident silence about the rest, so this truncates deliberately and
    names what it dropped.
    """
    from .reviews import assemble

    if from_dir:
        files = json.loads((from_dir / "files.json").read_text(encoding="utf-8"))
        pull = json.loads((from_dir / "pull.json").read_text(encoding="utf-8"))
    else:
        if not repo:
            _fail("--repo is required (or set GITHUB_REPOSITORY)")
        pull = _gh_json(f"repos/{repo}/pulls/{pr}")
        files = _gh_json(f"repos/{repo}/pulls/{pr}/files", "--paginate")

    diff = assemble(files, pull)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(diff, indent=2) + "\n", encoding="utf-8")

    _emit_output("files", str(len(diff["files"])))
    _emit_output("head_sha", diff["head_sha"])
    _emit_output("truncated", "true" if diff["truncated"] else "false")
    click.echo(f"{len(diff['files'])} file(s) for review -> {output}")
    if diff["not_reviewed"]:
        click.echo(f"not reviewed: {', '.join(diff['not_reviewed'][:5])}", err=True)


@main.command(name="review-state")
@click.option("--pr", required=True, help="Pull request number.")
@click.option("--repo", envvar="GITHUB_REPOSITORY", default="")
@click.option("--requested", default="", help="What the comment asked for. Empty reviews everything.")
@click.option("--available", required=True, help="The aspects this pipeline has a reviewer for.")
@click.option("--head", default="", help="The commit being reviewed. Read from the API if omitted.")
@click.option("--output-dir", required=True, type=click.Path(path_type=Path))
@click.option("--force", is_flag=True, help="Review again even if nothing has changed.")
@click.option("--from-dir", type=click.Path(path_type=Path), help="Read fixtures instead of the API.")
def review_state(
    pr: str,
    repo: str,
    requested: str,
    available: str,
    head: str,
    output_dir: Path,
    force: bool,
    from_dir: Path | None,
) -> None:
    """Work out which reviews are still due, and what each one is revising.

    A second review saying the same thing about the same commit is worse than no review: it buries
    the human conversation and teaches people to mute the bot.
    """
    from .reviews import ReviewError, plan, requested_aspects

    known = [name.strip() for name in available.split(",") if name.strip()]
    if not known:
        _fail("--available lists no aspects; nothing could ever be reviewed")
    try:
        aspects = [{"key": key} for key in requested_aspects(requested, known)]
    except ReviewError as error:
        _fail(str(error))

    if from_dir:

        def load(name: str) -> Any:
            path = from_dir / f"{name}.json"
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []

        reviews, commits = load("reviews"), load("commits")
        head = head or "head000"
    else:
        if not repo:
            _fail("--repo is required (or set GITHUB_REPOSITORY)")
        reviews = _gh_json(f"repos/{repo}/pulls/{pr}/reviews", "--paginate")
        commits = _gh_json(f"repos/{repo}/pulls/{pr}/commits", "--paginate")
        head = head or (commits[-1]["sha"] if commits else "")

    pending, skipped = plan(aspects, reviews, head, commits, force=force)

    # One file per aspect, because one job per aspect reads them. The reviewing job for an aspect
    # that is not pending never starts, so it never looks for a file that is not there.
    output_dir.mkdir(parents=True, exist_ok=True)
    for item in pending:
        item["head_sha"] = head
        (output_dir / f"{item['key']}.json").write_text(json.dumps(item, indent=2) + "\n", encoding="utf-8")

    # A JSON array, because the conditions gating each reviewing job test membership in it.
    _emit_output("pending", json.dumps([item["key"] for item in pending]))
    click.echo(f"{len(pending)} aspect(s) to review, {len(skipped)} unchanged")
    for entry in skipped:
        click.echo(f"  skipping {entry['key']}: {entry['reason']}", err=True)


@main.command(name="post-reviews")
@click.option("--pr", default="", help="Pull request number. Empty posts nothing.")
@click.option("--repo", envvar="GITHUB_REPOSITORY", default="")
@click.option("--reviews", required=True, type=click.Path(path_type=Path), help="One JSON per aspect.")
@click.option("--pending", type=click.Path(path_type=Path), help="What review-state wrote, for revisions.")
@click.option("--diff", type=click.Path(path_type=Path), help="The diff, for the commit reviewed.")
@click.option("--dry-run", is_flag=True, help="Render the bodies and post nothing.")
def post_reviews(
    pr: str, repo: str, reviews: Path, pending: Path | None, diff: Path | None, dry_run: bool
) -> None:
    """Publish each aspect's findings as its own pull request review, revising rather than repeating.

    Which review to revise is the pipeline's record rather than something the agent hands back. An
    agent that forgot to echo it would post beside its own earlier review instead of replacing it.
    """
    from .reviews import review_payload

    if not pr:
        click.echo("no pull request to post to")
        return
    files = sorted(reviews.glob("*.json")) if reviews.is_dir() else []
    if not files:
        click.echo("nothing to post: no aspect needed reviewing")
        return

    sha = ""
    if diff and diff.is_file():
        sha = str(json.loads(diff.read_text(encoding="utf-8")).get("head_sha") or "")

    posted = 0
    for path in files:
        aspect = path.stem
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            click.echo(f"::warning::{aspect}: the reviewer's output is not valid JSON", err=True)
            continue

        previous = ""
        record = (pending / f"{aspect}.json") if pending else None
        if record and record.is_file():
            previous = str(json.loads(record.read_text(encoding="utf-8")).get("previous_review_id") or "")

        action, payload = review_payload(aspect, result, sha=sha, previous_id=previous)
        if dry_run:
            click.echo(f"--- {aspect} ({action})\n{payload['body']}")
            posted += 1
            continue
        target = (
            f"repos/{repo}/pulls/{pr}/reviews/{previous}"
            if action == "revise"
            else f"repos/{repo}/pulls/{pr}/reviews"
        )
        method = "PUT" if action == "revise" else "POST"
        # A failure to post one aspect is not a reason to lose the others.
        if _gh_send(method, target, payload):
            posted += 1
            click.echo(f"{'revised' if action == 'revise' else 'posted'} the {aspect} review")
        else:
            click.echo(f"::warning::could not {action} the {aspect} review", err=True)

    click.echo(f"posted or revised {posted} review(s)")
    _summary(f"posted or revised {posted} review(s)")
    _emit_output("posted", str(posted))


def _gh_send(method: str, path: str, payload: dict[str, Any]) -> bool:
    result = subprocess.run(
        ["gh", "api", "-X", method, path, "--input", "-"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        click.echo(result.stderr.strip()[:300], err=True)
    return result.returncode == 0


@main.command(name="apply-patch")
@click.option("--patch", required=True, type=click.Path(path_type=Path))
@click.option("--repo", default=".", type=click.Path(path_type=Path), help="Where to apply it.")
@click.option("--check", is_flag=True, help="Report whether it would apply, without applying it.")
def apply_patch(patch: Path, repo: Path, check: bool) -> None:
    """Apply an agent-proposed diff, refusing anything that reaches outside the source tree.

    The agent that wrote this diff has no write permission and never touches the repository. This is
    the only thing that does, which is why the rules are here in code rather than in a prompt.
    """
    from .changes import protected_paths

    if not patch.is_file():
        _fail(f"{patch} does not exist")
    diff = patch.read_text(encoding="utf-8")
    if not diff.strip():
        click.echo("empty patch; nothing to apply")
        _emit_output("applied", "false")
        return

    breaches = protected_paths(diff)
    if breaches:
        _emit_output("applied", "false")
        _fail(f"patch touches protected paths and was rejected: {', '.join(breaches)}")

    command = ["git", "apply", "--check"] if check else ["git", "apply", "--3way", "--whitespace=nowarn"]
    result = subprocess.run(
        [*command, str(patch.resolve())], cwd=repo, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        click.echo(result.stderr.strip(), err=True)
        _emit_output("applied", "false")
        _fail("patch did not apply cleanly")

    click.echo("patch would apply" if check else "patch applied")
    _emit_output("applied", "false" if check else "true")


@main.command(name="run-suite")
@click.option("--repo", default=".", type=click.Path(path_type=Path))
@click.option(
    "--overlay",
    type=click.Path(path_type=Path),
    default=None,
    help="A directory of proposed files to lay over the repository before running.",
)
@click.option("--suite", default="pytest", show_default=True, help="pytest, jest, go or cargo.")
@click.option("--select", default="", help="Narrow the run, e.g. one reproducer test.")
@click.option("--output", type=click.Path(path_type=Path), help="Write the verdict as JSON.")
@click.option(
    "--expect",
    type=click.Choice(["pass", "fail"]),
    default="pass",
    show_default=True,
    help="What this step needs the suite to do.",
)
def run_suite(
    repo: Path, overlay: Path | None, suite: str, select: str, output: Path | None, expect: str
) -> None:
    """Run the target project's own tests and turn the result into a verdict.

    `--expect fail` is what makes a reproducer meaningful: a test that does not fail before the fix
    proves nothing about the bug, so a pipeline asserts the failure first and the pass afterwards.
    """
    import shutil

    from .changes import SUITES, suite_verdict

    if overlay and overlay.is_dir():
        # The same copy `propose-pr` will make later, made now so the suite runs against what the
        # pull request would contain. Without it, "prove the reproducer fails" would run the
        # repository as it already is and pass for the wrong reason.
        shutil.copytree(overlay, repo, dirs_exist_ok=True)
        click.echo(f"laid {overlay} over {repo}")

    if suite not in SUITES:
        _fail(f"unknown suite {suite!r} — known: {', '.join(sorted(SUITES))}")
    command = [*SUITES[suite], *(select.split() if select else [])]
    result = subprocess.run(command, cwd=repo, capture_output=True, text=True, check=False)
    verdict = suite_verdict(result, suite=suite, select=select, expect=expect)

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(verdict, indent=2) + "\n", encoding="utf-8")
    _emit_output("passed", "true" if verdict["passed"] else "false")
    _emit_output("satisfied", "true" if verdict["satisfied"] else "false")
    click.echo(f"{suite}: {'passed' if verdict['passed'] else 'failed'} (expected to {expect})")

    if not verdict["satisfied"]:
        _fail(f"suite was expected to {expect} and did not")


@main.command(name="await-checks")
@click.option("--ref", required=True, help="Commit SHA or branch whose checks to wait for.")
@click.option("--repo", envvar="GITHUB_REPOSITORY", required=True)
@click.option("--timeout", default=1800, show_default=True, type=int, help="Seconds.")
@click.option("--interval", default=20, show_default=True, type=int)
@click.option("--ignore", default="", help="Comma-separated check names to disregard.")
@click.option("--output", type=click.Path(path_type=Path))
def await_checks(ref: str, repo: str, timeout: int, interval: int, ignore: str, output: Path | None) -> None:
    """Wait for the repository's own CI to finish, and report what it concluded.

    A pipeline writes a change and then asks the project's real CI whether it holds up — not a test
    command this pipeline chose, which would only prove the change satisfies this pipeline.
    """
    import time

    from .changes import check_verdict

    skip = {name.strip() for name in ignore.split(",") if name.strip()}
    deadline = time.monotonic() + timeout
    runs: list[dict[str, Any]] = []

    while time.monotonic() < deadline:
        payload = _gh_json(f"repos/{repo}/commits/{ref}/check-runs")
        runs = [r for r in payload.get("check_runs", []) if r.get("name") not in skip]
        if runs and all(run.get("status") == "completed" for run in runs):
            break
        waiting = sum(1 for r in runs if r.get("status") != "completed")
        click.echo(f"waiting: {waiting} still running", err=True)
        time.sleep(interval)
    else:
        _fail(f"checks on {ref} did not finish within {timeout}s")

    verdict = check_verdict(runs, ref=ref)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(verdict, indent=2) + "\n", encoding="utf-8")

    _emit_output("passed", "true" if verdict["passed"] else "false")
    _emit_output("failed", ",".join(verdict["failed"]))
    click.echo(
        f"{len(runs)} check(s): "
        + ("all passed" if verdict["passed"] else "failed: " + ", ".join(verdict["failed"]))
    )
    if not verdict["passed"]:
        _fail("the repository's own CI rejected this change")


@main.command(name="render-plan")
@click.option("--plan", required=True, type=click.Path(path_type=Path))
@click.option("--output", required=True, type=click.Path(path_type=Path))
def render_plan_command(plan: Path, output: Path) -> None:
    """Render a plan as the markdown a reviewer reads on the pull request.

    Deterministic on purpose: the agent decided what the plan says, and layout that varied between
    runs would make a comment updated in place churn for no reason.
    """
    from .changes import load_plan, render_plan

    if not plan.is_file():
        click.echo("no plan to render")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_plan(load_plan(plan)), encoding="utf-8")
    click.echo(f"rendered the plan -> {output}")


@main.command(name="jira-update")
@click.option("--issue", required=True, help="The issue key to write to.")
@click.option("--from", "source", required=True, type=click.Path(path_type=Path), help="Agent output.")
@click.option("--base-url", envvar="JIRA_BASE_URL", default="")
@click.option(
    "--name",
    default="",
    help="Distinguishes this pipeline's comment marker when several comment on one issue.",
)
@click.option("--comment", "post_comment", is_flag=True, help="Post or revise the `comment` field.")
@click.option("--labels", "add_labels", is_flag=True, help="Add the `labels` field.")
@click.option("--priority", "set_priority", is_flag=True, help="Set the `priority` field.")
@click.option("--max-labels", default=5, show_default=True, type=int, help="Cap on labels per run.")
@click.option("--dry-run", is_flag=True, help="Report what would be written and write nothing.")
def jira_update(
    issue: str,
    source: Path,
    base_url: str,
    name: str,
    post_comment: bool,
    add_labels: bool,
    set_priority: bool,
    max_labels: int,
    dry_run: bool,
) -> None:
    """Write an agent's conclusions back to a Jira issue.

    The counterpart to gh-aw's safe outputs, which is how the same conclusions reach a GitHub issue.
    The shape is deliberately the same: the agent writes a JSON file and never holds a credential,
    and this deterministic step is the only thing that writes.

    Every write is additive. Labels are *added* — sending a replacement set would silently delete
    whatever a person put on the issue, which is the most destructive thing a write-back could do
    and the easiest to do by accident. Nothing here transitions an issue: a transition fires
    workflow automation, notifications and SLA timers, and a triage bot should not be starting
    those. Add a step of your own if you want that, deliberately.

    Commenting is idempotent. The comment carries a marker, and a second run revises the comment it
    left last time rather than posting beside it.
    """
    from .issues import JiraWriteError, find_marked_comment, label_update, priority_update, with_marker

    if not source.is_file():
        _fail(f"{source} does not exist — the agent wrote nothing to write back")
    try:
        result = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        _fail(f"{source} is not valid JSON ({error.msg})")
    if not isinstance(result, dict):
        _fail(f"{source} holds {type(result).__name__}, not an object")

    if not (post_comment or add_labels or set_priority):
        _fail("nothing asked for — pass --comment, --labels or --priority")

    token = os.environ.get("JIRA_API_TOKEN", "")
    if not dry_run and (not base_url or not token):
        _fail("JIRA_BASE_URL and JIRA_API_TOKEN are required unless --dry-run is given")

    written: list[str] = []
    fields: dict[str, Any] = {}
    try:
        if add_labels and result.get("labels"):
            fields.update(label_update(list(result["labels"]), cap=max_labels))
        if set_priority and result.get("priority"):
            payload = priority_update(str(result["priority"]))
            fields.setdefault("fields", {}).update(payload["fields"])
    except JiraWriteError as error:
        _fail(str(error))

    if fields:
        if dry_run:
            click.echo(f"would update {issue}: {json.dumps(fields)}")
            written.append("fields")
        elif _jira_send("PUT", base_url, token, f"issue/{issue}", fields):
            written.append("fields")
        else:
            _fail(f"could not update {issue}")

    if post_comment and str(result.get("comment") or "").strip():
        body = with_marker(str(result["comment"]), name=name)
        existing = None
        if not dry_run:
            payload = _jira_get(base_url, token, f"issue/{issue}/comment") or {}
            existing = find_marked_comment(list(payload.get("comments") or []), name=name)
        if dry_run:
            click.echo(f"would comment on {issue}:\n{body}")
            written.append("comment")
        else:
            path = f"issue/{issue}/comment/{existing['id']}" if existing else f"issue/{issue}/comment"
            method = "PUT" if existing else "POST"
            if _jira_send(method, base_url, token, path, {"body": body}):
                written.append("revised comment" if existing else "comment")
            else:
                _fail(f"could not comment on {issue}")

    _emit_output("written", ",".join(written))
    verb = "would write" if dry_run else "wrote"
    click.echo(f"{issue}: " + (f"{verb} {', '.join(written)}" if written else "nothing to write"))


def _jira_request(method: str, base_url: str, token: str, path: str, payload: Any) -> Any:
    import urllib.error
    import urllib.request

    url = f"{base_url.rstrip('/')}/rest/api/2/{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


def _jira_send(method: str, base_url: str, token: str, path: str, payload: dict[str, Any]) -> bool:
    import urllib.error

    try:
        _jira_request(method, base_url, token, path, payload)
    except urllib.error.HTTPError as error:
        # Jira answers a rejected write with a body naming the field; the status alone does not.
        detail = error.read().decode("utf-8", errors="replace")[:300]
        click.echo(click.style(f"{method} {path} -> {error.code}: {detail}", fg="red"), err=True)
        return False
    except (urllib.error.URLError, OSError) as error:
        click.echo(click.style(f"{method} {path} failed: {error}", fg="red"), err=True)
        return False
    return True


def _jira_get(base_url: str, token: str, path: str) -> Any:
    import urllib.error

    try:
        return _jira_request("GET", base_url, token, path, None)
    except (urllib.error.URLError, OSError):
        # Not fatal: without the existing comments this posts a new one rather than revising, which
        # is a duplicate comment rather than a failed run.
        click.echo(f"could not read existing comments on {path}", err=True)
        return {}


@main.command(name="run-history")
@click.option(
    "--ledger",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory of month files. Omit when reading from --branch.",
)
@click.option("--branch", default="", help="Read the ledger from this branch instead of a directory.")
@click.option("--path", default="history", show_default=True, help="Directory within that branch.")
@click.option("--output", required=True, type=click.Path(path_type=Path))
@click.option("--since", default="", help="ISO timestamp splitting the baseline from the window.")
@click.option("--days", default=0, type=int, help="Window size in days, if --since is not given.")
@click.option("--min-runs", default=5, show_default=True, type=int, help="Below this, report no trend.")
def run_history(
    ledger: Path | None, branch: str, path: str, output: Path, since: str, days: int, min_runs: int
) -> None:
    """Read the run ledger and report what moved.

    The ledger answers the one question a dashboard cannot: is this getting better or worse. That
    needs two windows, so everything here is a comparison — and a window with too few runs in it is
    reported as *too few runs* rather than as a direction, because noise presented as a trend is
    worse than silence. Somebody acts on it.
    """
    from datetime import timedelta

    from .history import LedgerError, Report, materialize_branch, read_ledger

    if branch:
        try:
            ledger = materialize_branch(branch, path=path)
        except LedgerError as error:
            _fail(f"could not read the ledger branch {branch!r}: {error}")
    if ledger is None:
        _fail("pass --ledger or --branch")
    if not ledger.is_dir():
        _fail(f"no ledger at {ledger} — nothing has been recorded yet")
    records = read_ledger(ledger)
    if not records:
        _fail(f"no run records under {ledger}")

    if not since and days:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")

    report = Report(records=records, since=since).build(min_runs=min_runs)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    window, totals = report["window"], report["totals"]
    _emit_output("runs", str(totals["runs"]))
    _emit_output("compared", "true" if window["compared"] else "false")
    _emit_output("outliers", str(len(report["outliers"])))
    click.echo(
        f"{totals['runs']} run(s), {totals['failed_runs']} failed, ${totals['cost_usd']:,.2f} -> {output}"
    )
    if not window["compared"]:
        # A snapshot read as a trend is how a first month becomes a regression.
        click.echo("no baseline window: this is a snapshot, not a comparison")
    for entry in report["outliers"]:
        click.echo(f"  outlier: {entry['workflow']} run {entry['run_id']} — {entry['times_median']}x median")


def _record_eval(report: dict[str, Any], history_dir: Path, prompt_file: Path | None) -> None:
    """Append this suite run to the ledger, fingerprinted by the prompt it scored.

    Without the fingerprint the record is unusable: a baseline is only a baseline if it ran the
    prompt this one replaces, and a ledger that mixed several prompts together would call their
    differences noise.
    """
    from datetime import UTC, datetime

    from .improvement import eval_record, fingerprint
    from .otel import history_file, history_line

    prompt = fingerprint(prompt_file) if prompt_file else ""
    if not prompt:
        click.echo(
            "not recording this run: no prompt fingerprint, so nothing could be compared to it",
            err=True,
        )
        return

    finished = datetime.now(UTC).isoformat(timespec="seconds")
    entry = eval_record(
        report,
        prompt=prompt,
        identity={
            "run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "run_url": (
                f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/"
                f"{os.environ.get('GITHUB_RUN_ID', '')}"
            ),
            "ref": os.environ.get("GITHUB_REF_NAME", ""),
            "sha": os.environ.get("GITHUB_SHA", ""),
            "finished": finished,
        },
    )
    target = history_dir / Path(history_file(finished)).name
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(history_line(entry))
    click.echo(f"recorded {entry['agent']} at prompt {prompt} -> {target}")


@main.command(name="eval-compare")
@click.option("--agent", required=True, help="Whose suite this is.")
@click.option("--report", required=True, type=click.Path(path_type=Path), help="This run's report.")
@click.option("--prompt-file", required=True, type=click.Path(path_type=Path))
@click.option("--ledger", type=click.Path(path_type=Path), help="Directory of recorded runs.")
@click.option("--branch", default="", help="Read the ledger from this branch instead.")
@click.option("--path", default="history", show_default=True, help="Directory within that branch.")
@click.option("--output", required=True, type=click.Path(path_type=Path))
@click.option(
    "--fail-on-regression",
    is_flag=True,
    help="Exit non-zero when a case that passed every baseline run now fails.",
)
def eval_compare(
    agent: str,
    report: Path,
    prompt_file: Path,
    ledger: Path | None,
    branch: str,
    path: str,
    output: Path,
    fail_on_regression: bool,
) -> None:
    """Say whether a change to a prompt made the agent better, and how confidently.

    The baseline is what the previous prompt scored — the default branch already ran it, so nothing
    here pays to re-run an old prompt. The noise floor is the spread across those runs, which
    differed by nothing except sampling, and a delta smaller than it is reported as noise rather
    than as a direction.

    A comparison that does not know its own noise floor is an opinion with arithmetic on it, so a
    baseline too thin to measure one says so instead of reporting a verdict.
    """
    from .history import LedgerError, materialize_branch
    from .improvement import Comparison, baseline_runs, eval_record, fingerprint, read_eval_records, render
    from .improvement import load as load_report

    prompt = fingerprint(prompt_file)
    if not prompt:
        _fail(f"no compiled agent at {prompt_file} — nothing to fingerprint")

    directory = ledger
    if branch:
        try:
            directory = materialize_branch(branch, path=path)
        except LedgerError as error:
            # A branch that does not exist yet is the *first run*, not a failure. This is the day-one
            # path on every repository that turns the loop on, and treating it as a usage error
            # would fail the very first eval before there was anything it could have compared.
            click.echo(f"no ledger on {branch!r} yet ({error})", err=True)
            directory = None
    elif ledger is None:
        _fail("pass --ledger or --branch")

    records = read_eval_records(directory, agent=agent) if directory and directory.is_dir() else []
    candidate = eval_record(load_report(report), prompt=prompt, identity={})
    comparison = Comparison(
        agent=agent, candidate=candidate, runs=baseline_runs(records, candidate_prompt=prompt)
    ).build()

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(comparison, indent=2) + "\n", encoding="utf-8")

    rendered = render(comparison)
    click.echo(rendered)
    _summary(rendered)
    _emit_output("verdict", comparison["verdict"])
    _emit_output("regressed", ",".join(comparison["regressed"]))
    _emit_output("noise_measured", "true" if comparison["baseline"]["noise_measured"] else "false")

    if fail_on_regression and comparison["regressed"]:
        _fail(
            f"{len(comparison['regressed'])} case(s) that passed every baseline run now fail: "
            + ", ".join(comparison["regressed"])
        )
