"""Writing, pruning, and the drift gate."""

from __future__ import annotations

from lockstep.emit import compile_spec
from lockstep.emit.writer import check_plan, previously_generated, write_plan


def test_write_then_check_is_clean(basic_root):
    plan = compile_spec(basic_root)
    report = write_plan(basic_root, plan)
    assert report.created
    assert not report.updated

    assert check_plan(basic_root, compile_spec(basic_root)).clean


def test_second_write_changes_nothing(basic_root):
    write_plan(basic_root, compile_spec(basic_root))
    report = write_plan(basic_root, compile_spec(basic_root))
    assert not report.changed
    assert report.unchanged


def test_check_detects_a_hand_edited_file(basic_root):
    write_plan(basic_root, compile_spec(basic_root))
    target = basic_root / ".github/workflows/discover.yml"
    target.write_text(target.read_text() + "\n# sneaky\n")

    report = check_plan(basic_root, compile_spec(basic_root))
    assert not report.clean
    assert ".github/workflows/discover.yml" in report.modified


def test_check_detects_a_missing_file(basic_root):
    write_plan(basic_root, compile_spec(basic_root))
    (basic_root / ".github/workflows/discover.yml").unlink()
    assert ".github/workflows/discover.yml" in check_plan(basic_root, compile_spec(basic_root)).missing


def test_check_detects_a_spec_edit_without_recompiling(basic_root):
    write_plan(basic_root, compile_spec(basic_root))
    command = basic_root / "commands" / "discover.md"
    command.write_text(command.read_text().replace("Discover UI structure", "Map the UI"))
    assert not check_plan(basic_root, compile_spec(basic_root)).clean


def test_orphans_are_pruned_when_a_command_is_removed(basic_root):
    write_plan(basic_root, compile_spec(basic_root))
    assert ".github/workflows/discover.yml" in previously_generated(basic_root)

    nested_step = (
        "1. **Discover application structure** → command: discover\n"
        "   - profile: my-app\n"
        "   (if not --skip-discovery)\n\n"
    )
    command = basic_root / "commands" / "generate-tests.md"
    command.write_text(command.read_text().replace(nested_step, ""))
    (basic_root / "commands" / "discover.md").unlink()

    report = write_plan(basic_root, compile_spec(basic_root))
    assert ".github/workflows/discover.yml" in report.removed
    assert not (basic_root / ".github/workflows/discover.yml").exists()


def test_ejected_files_are_left_alone(basic_root):
    write_plan(basic_root, compile_spec(basic_root))
    target = basic_root / ".github/workflows/discover.yml"
    target.write_text("# EJECTED — hand maintained\n")
    (basic_root / ".pipeline" / "ejected.yaml").write_text("files:\n  - .github/workflows/discover.yml\n")

    write_plan(basic_root, compile_spec(basic_root))
    assert target.read_text() == "# EJECTED — hand maintained\n"
    assert check_plan(basic_root, compile_spec(basic_root)).clean


def test_compile_manifest_records_every_generated_file(basic_root):
    plan = compile_spec(basic_root)
    write_plan(basic_root, plan)
    recorded = previously_generated(basic_root)
    assert ".github/workflows/aw-story-extractor.md" in recorded
    assert "SECRETS.md" in recorded
