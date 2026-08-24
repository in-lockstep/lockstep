"""Where capabilities live, and what a pin is worth.

Two failures this exists to prevent. A compiler with a built-in default points a workflow at a
repository the author never chose. And a pin of forty zeros looks exactly like a pin, right up until
a runner tries to fetch it.
"""

from __future__ import annotations

import json

import pytest

from conftest import ready_but_unpublished
from lockstep.checks import doctor
from lockstep.emit import compile_spec
from lockstep.emit.context import PLACEHOLDER_DIGEST, PLACEHOLDER_SHA, Pins
from lockstep.errors import EmitError
from lockstep.lifecycle import pin, resolve_ref, write_pins
from lockstep.spec.load import load_spec

SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
QUAY = {"exec_image: ghcr.io/in-lockstep/pipeline-exec": "exec-image: quay.io/acme/exec"}
UNSET = {"exec_image: ghcr.io/in-lockstep/pipeline-exec": ""}


def manifest(root, **replacements):
    path = root / "pipeline.yaml"
    text = path.read_text()
    for old, new in replacements.items():
        text = text.replace(old.replace("_", "-"), new)
    path.write_text(text, encoding="utf-8")


def lock(root):
    return json.loads((root / ".pipeline" / "pins.lock").read_text())


def write_lock(root, data):
    (root / ".pipeline" / "pins.lock").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def resolved(root):
    """The same fixture with real pins, so tests can isolate one failure at a time."""
    data, _, _ = pin(load_spec(root), root, actions_sha=SHA, exec_digest=DIGEST, offline=True)
    write_pins(root, data)
    return root


# --- the registry is the manifest's choice ----------------------------------


def test_any_registry_works(basic_root):
    manifest(basic_root, **QUAY)
    data = lock(basic_root)
    data["capabilities"]["exec"]["image"] = "quay.io/acme/exec"
    write_lock(basic_root, data)
    workflow = compile_spec(basic_root).files[".github/workflows/generate-tests.yml"]
    assert f"quay.io/acme/exec@{PLACEHOLDER_DIGEST}" in workflow


def test_pin_writes_the_manifests_registry_into_the_lock(basic_root):
    manifest(basic_root, **QUAY)
    data, _, _ = pin(load_spec(basic_root), basic_root, offline=True)
    assert data["capabilities"]["exec"]["image"] == "quay.io/acme/exec"


def test_moving_the_image_drops_the_digest_that_belonged_to_the_old_one(basic_root):
    resolved(basic_root)
    manifest(basic_root, **QUAY)
    data, notes, unresolved = pin(load_spec(basic_root), basic_root, offline=True)
    assert "digest" not in data["capabilities"]["exec"]
    assert any("moved to quay.io/acme/exec" in note for note in notes)
    assert any("exec image" in item for item in unresolved)


def test_a_lock_resolved_against_a_different_image_is_refused(basic_root):
    """Preferring either side silently would run a digest from a registry nobody asked for."""
    manifest(basic_root, **QUAY)
    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    rendered = excinfo.value.render()
    assert "quay.io/acme/exec" in rendered
    assert "lockstep pin" in rendered


def test_a_lock_resolved_against_a_different_actions_repo_is_refused(basic_root):
    moved = {
        "actions: github.com/in-lockstep/lockstep/actions@actions-v1.6.2": (
            "actions: github.com/acme/actions@v1.6.2"
        )
    }
    manifest(basic_root, **moved)
    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    assert "acme/actions" in excinfo.value.render()


# --- no built-in default ----------------------------------------------------


def test_an_unset_image_is_refused_rather_than_guessed(basic_root):
    manifest(basic_root, **UNSET)
    data = lock(basic_root)
    data["capabilities"]["exec"].pop("image")
    write_lock(basic_root, data)
    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    assert "capabilities.exec-image" in excinfo.value.render()


def test_an_unset_actions_repo_is_refused_rather_than_guessed(basic_root):
    manifest(basic_root, **{"actions: github.com/in-lockstep/lockstep/actions@actions-v1.6.2": ""})
    data = lock(basic_root)
    data["capabilities"].pop("actions")
    write_lock(basic_root, data)
    with pytest.raises(EmitError) as excinfo:
        compile_spec(basic_root)
    assert "capabilities.actions" in excinfo.value.render()


def test_doctor_names_a_missing_image(basic_root):
    manifest(basic_root, **UNSET)
    data = lock(basic_root)
    data["capabilities"]["exec"].pop("image")
    write_lock(basic_root, data)
    codes = {finding.code for finding in doctor(load_spec(basic_root), basic_root).findings}
    assert "DOC016" in codes


# --- a placeholder is not a pin ---------------------------------------------


def test_a_zero_pin_is_reported_as_a_placeholder(basic_spec_dir):
    pins = Pins.load(load_spec(basic_spec_dir))
    assert pins.exec_digest == PLACEHOLDER_DIGEST
    assert pins.placeholders() == ["executor image (ghcr.io/in-lockstep/pipeline-exec)"]


def test_the_check_catches_zeros_and_not_invention(basic_spec_dir):
    """A limit worth stating: the fixture's actions SHA is made up, and looks entirely real.

    Nothing offline can tell a fabricated commit from one that exists. `lockstep pin` contacting the
    remote is the only thing that can, which is why it reports what it could not resolve rather than
    leaving a plausible value in place.
    """
    pins = Pins.load(load_spec(basic_spec_dir))
    assert pins.actions_sha != PLACEHOLDER_SHA
    assert not [entry for entry in pins.placeholders() if "actions" in entry]


def test_no_example_still_pins_a_placeholder(repo_root):
    """The inverse of what this asserted before the capabilities were published.

    It used to require every example to admit it could not run, because a zero has the shape of a
    pin and nothing else would have said so. `actions-v0.1.0` and `exec-v0.1.0` exist now, so the
    same test earns its keep pointing the other way: an example that drifts back to a placeholder is
    one that compiles, lints and simulates exactly like the others and cannot run.
    """
    for example in sorted((repo_root / "examples").iterdir()):
        if not (example / "pipeline.yaml").is_file():
            continue
        assert Pins.load(load_spec(example)).placeholders() == [], example.name


def test_doctor_treats_a_placeholder_as_unpinned(basic_spec_dir):
    ready_but_unpublished(doctor(load_spec(basic_spec_dir), basic_spec_dir))


def test_compile_says_so_on_every_run(basic_spec_dir):
    """Output referencing a placeholder looks exactly like output that runs."""
    notes = compile_spec(basic_spec_dir).notes
    assert [note for note in notes if "cannot run as emitted" in note]


def test_a_real_pin_is_not_a_placeholder(basic_root):
    resolved(basic_root)
    pins = Pins.load(load_spec(basic_root))
    assert pins.placeholders() == []
    assert doctor(load_spec(basic_root), basic_root).ok
    assert not [note for note in compile_spec(basic_root).notes if "placeholder" in note]


# --- resolving a ref to a commit ------------------------------------------------------------------
#
# These exist because the first real tag this project ever cut was annotated, and `lockstep pin`
# recorded the tag *object* for it. Actions resolves `uses: owner/repo/action@<sha>` against commits
# only, so the pin was forty plausible characters naming something nothing can check out — the exact
# class of failure pinning exists to prevent, in the one place nobody was looking.


def _ls_remote(monkeypatch, stdout: str):
    import subprocess

    def fake_run(argv, **kwargs):
        assert argv[:2] == ["git", "ls-remote"]
        # The peeled line has to be asked for by name: a pattern matching `<ref>` does not match
        # `<ref>^{}`. If this stops being requested, the resolution silently loses the commit.
        assert any(arg.endswith("^{}") for arg in argv), argv
        assert "--refs" not in argv, "--refs suppresses the peeled line entirely"
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(subprocess, "run", fake_run)


TAG_OBJECT = "a103eeb4316721ee42ce32076864f628bdf0d81a"
COMMIT = "aad2f112b8263ca9cf9a6169e1247d02d6ba9e7a"


def test_an_annotated_tag_resolves_to_its_commit_not_its_tag_object(monkeypatch):
    _ls_remote(
        monkeypatch,
        f"{TAG_OBJECT}\trefs/tags/actions-v0.1.0\n{COMMIT}\trefs/tags/actions-v0.1.0^{{}}\n",
    )
    assert resolve_ref("in-lockstep/lockstep/actions", "actions-v0.1.0") == COMMIT


def test_the_order_of_the_lines_does_not_decide_it(monkeypatch):
    """git makes no promise about which line comes first, and the old code took whichever did."""
    _ls_remote(
        monkeypatch,
        f"{COMMIT}\trefs/tags/actions-v0.1.0^{{}}\n{TAG_OBJECT}\trefs/tags/actions-v0.1.0\n",
    )
    assert resolve_ref("in-lockstep/lockstep/actions", "actions-v0.1.0") == COMMIT


def test_a_lightweight_tag_has_no_peeled_line_and_is_already_the_commit(monkeypatch):
    _ls_remote(monkeypatch, f"{COMMIT}\trefs/tags/actions-v0.1.0\n")
    assert resolve_ref("in-lockstep/lockstep/actions", "actions-v0.1.0") == COMMIT


def test_a_branch_resolves_to_its_head(monkeypatch):
    _ls_remote(monkeypatch, f"{COMMIT}\trefs/heads/main\n")
    assert resolve_ref("acme/standards", "main") == COMMIT
