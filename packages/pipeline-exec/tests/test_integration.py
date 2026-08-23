"""The full fan-out cycle, end to end: fan out, run the legs, then verify coverage."""

from __future__ import annotations

import json

from click.testing import CliRunner
from pipeline_exec.cli import main

FAILURES = [
    {"key": "test-login", "reason": "selector"},
    {"key": "test-signup", "reason": "timeout"},
    {"key": "test-logout", "reason": "selector"},
]


def run(*args):
    return CliRunner().invoke(main, list(args))


def outputs(result):
    return dict(line.split("=", 1) for line in result.output.strip().splitlines() if "=" in line)


def setup(tmp_path):
    source = tmp_path / "failures.json"
    source.write_text(json.dumps(FAILURES), encoding="utf-8")
    out = tmp_path / "repairs"
    out.mkdir()
    return source, out


def repair_command(out):
    return ["sh", "-c", f"echo '{{item.reason}}' > {out}/{{item.key}}.json"]


def test_a_complete_cycle_reaches_full_coverage(tmp_path):
    source, out = setup(tmp_path)
    legs = json.loads(
        outputs(run("fanout", f"--input={source}", "--only-missing", f"--output-dir={out}"))["items"]
    )
    assert len(legs) == 3

    for leg in legs:
        result = run(
            "shard-run", f"--slice={json.dumps(leg)}", f"--input={source}", "--", *repair_command(out)
        )
        assert result.exit_code == 0

    verify = run("fanout-verify", f"--dir={out}", f"--expected={json.dumps(FAILURES)}")
    assert verify.exit_code == 0
    assert "coverage 3/3 (100%)" in verify.output


def test_a_resumed_run_only_fans_out_what_is_still_missing(tmp_path):
    """Each leg's output lands as it completes, so an interrupted run resumes where it stopped."""
    source, out = setup(tmp_path)
    (out / "test-login.json").write_text("done", encoding="utf-8")

    legs = json.loads(
        outputs(run("fanout", f"--input={source}", "--only-missing", f"--output-dir={out}"))["items"]
    )
    assert [leg["key"] for leg in legs] == ["test-signup", "test-logout"]


def test_sharded_legs_cover_the_same_work_as_item_legs(tmp_path):
    """Sharding is a packaging decision, not a semantic one: coverage must come out identical."""
    source, out = setup(tmp_path)
    shards = json.loads(
        outputs(run("fanout", f"--input={source}", "--shard-threshold=2", "--shards=2"))["items"]
    )
    assert len(shards) == 2

    for shard in shards:
        assert (
            run(
                "shard-run", f"--slice={json.dumps(shard)}", f"--input={source}", "--", *repair_command(out)
            ).exit_code
            == 0
        )

    assert sorted(path.name for path in out.iterdir()) == [
        "test-login.json",
        "test-logout.json",
        "test-signup.json",
    ]
    assert run("fanout-verify", f"--dir={out}", f"--expected={json.dumps(FAILURES)}").exit_code == 0


def test_a_failing_leg_leaves_the_others_covered(tmp_path):
    source, out = setup(tmp_path)
    command = ["sh", "-c", f"test '{{item.key}}' != test-signup && echo ok > {out}/{{item.key}}.json"]

    result = run("shard-run", '--slice={"shard":0,"shards":1}', f"--input={source}", "--", *command)
    assert result.exit_code == 1
    assert "failed items: test-signup" in result.output

    tolerant = run(
        "fanout-verify", f"--dir={out}", f"--expected={json.dumps(FAILURES)}", "--min-success-rate=0.6"
    )
    assert tolerant.exit_code == 0
    strict = run("fanout-verify", f"--dir={out}", f"--expected={json.dumps(FAILURES)}")
    assert strict.exit_code == 1
