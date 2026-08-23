"""The extension's own tests.

`apply-patch` is a trust boundary — it decides whether an agent's diff may land — so its refusals
matter more than its successes and are tested first.
"""

from __future__ import annotations

import json
import subprocess

from click.testing import CliRunner

from bugfix_ext.commands import apply_patch, protected_paths, run_suite

FIX = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
 def total(items):
-    return sum(items)
+    return sum(items or [])
"""

ESCAPE = """diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -1 +1 @@
-on: [push]
+on: [push, pull_request]
"""


def repo_with_source(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("def total(items):\n    return sum(items)\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "base"],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


# --- the trust boundary ----------------------------------------------------


def test_a_patch_touching_ci_configuration_is_detected():
    assert protected_paths(ESCAPE) == [".github/workflows/ci.yml"]


def test_a_patch_touching_the_pipeline_spec_is_detected():
    diff = ESCAPE.replace(".github/workflows/ci.yml", "commands/fix-bugs.md")
    assert protected_paths(diff) == ["commands/fix-bugs.md"]


def test_an_ordinary_source_patch_is_allowed():
    assert protected_paths(FIX) == []


def test_applying_a_patch_that_escapes_the_source_tree_is_refused(tmp_path):
    """The agent has no write permission; this is the only thing that writes, so it decides."""
    repo = repo_with_source(tmp_path)
    patch = tmp_path / "escape.patch"
    patch.write_text(ESCAPE, encoding="utf-8")

    result = CliRunner().invoke(apply_patch, [f"--patch={patch}", f"--repo={repo}"])
    assert result.exit_code == 1
    assert "protected paths" in result.output


# --- applying ---------------------------------------------------------------


def test_a_clean_patch_applies(tmp_path):
    repo = repo_with_source(tmp_path)
    patch = tmp_path / "fix.patch"
    patch.write_text(FIX, encoding="utf-8")

    result = CliRunner().invoke(apply_patch, [f"--patch={patch}", f"--repo={repo}"])
    assert result.exit_code == 0
    assert "sum(items or [])" in (repo / "src" / "app.py").read_text()


def test_check_mode_leaves_the_tree_alone(tmp_path):
    repo = repo_with_source(tmp_path)
    patch = tmp_path / "fix.patch"
    patch.write_text(FIX, encoding="utf-8")

    result = CliRunner().invoke(apply_patch, [f"--patch={patch}", f"--repo={repo}", "--check"])
    assert result.exit_code == 0
    assert "sum(items)" in (repo / "src" / "app.py").read_text()


def test_a_patch_that_does_not_apply_fails_loudly(tmp_path):
    repo = repo_with_source(tmp_path)
    patch = tmp_path / "stale.patch"
    patch.write_text(FIX.replace("return sum(items)", "return sum(numbers)"), encoding="utf-8")

    result = CliRunner().invoke(apply_patch, [f"--patch={patch}", f"--repo={repo}"])
    assert result.exit_code == 1


def test_an_empty_patch_is_not_a_failure(tmp_path):
    patch = tmp_path / "empty.patch"
    patch.write_text("", encoding="utf-8")
    result = CliRunner().invoke(apply_patch, [f"--patch={patch}", f"--repo={tmp_path}"])
    assert result.exit_code == 0
    assert "applied=false" in result.output


# --- the suite verdict ------------------------------------------------------


def test_a_reproducer_is_only_meaningful_if_it_fails_first(tmp_path):
    """`--expect fail` is what proves the test actually reproduces the bug."""
    (tmp_path / "test_x.py").write_text("def test_broken():\n    assert False\n", encoding="utf-8")
    verdict = tmp_path / "verdict.json"

    result = CliRunner().invoke(run_suite, [f"--repo={tmp_path}", "--expect=fail", f"--output={verdict}"])
    assert result.exit_code == 0
    assert json.loads(verdict.read_text())["satisfied"] is True


def test_a_reproducer_that_passes_before_the_fix_is_rejected(tmp_path):
    (tmp_path / "test_x.py").write_text("def test_fine():\n    assert True\n", encoding="utf-8")
    result = CliRunner().invoke(run_suite, [f"--repo={tmp_path}", "--expect=fail"])
    assert result.exit_code == 1
    assert "expected to fail" in result.output


def test_the_suite_must_pass_after_the_fix(tmp_path):
    (tmp_path / "test_x.py").write_text("def test_fixed():\n    assert True\n", encoding="utf-8")
    result = CliRunner().invoke(run_suite, [f"--repo={tmp_path}", "--expect=pass"])
    assert result.exit_code == 0
    assert "passed=true" in result.output


def test_the_verdict_records_enough_to_diagnose(tmp_path):
    (tmp_path / "test_x.py").write_text("def test_broken():\n    assert False\n", encoding="utf-8")
    verdict = tmp_path / "verdict.json"
    CliRunner().invoke(run_suite, [f"--repo={tmp_path}", "--expect=fail", f"--output={verdict}"])

    recorded = json.loads(verdict.read_text())
    assert recorded["suite"] == "pytest"
    assert recorded["passed"] is False
    assert "assert False" in recorded["output"] or "test_broken" in recorded["output"]
