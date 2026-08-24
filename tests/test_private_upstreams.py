"""Reading an upstream that is private.

A consumer's own `GITHUB_TOKEN` is scoped to the repository it belongs to, so it cannot read another
repository at all — public or private, the answer is the same and it is no. A private upstream
therefore needs a credential from somewhere else, and until this the framework said so in a docs
footnote and left every consuming repository to re-derive the wiring.
"""

from __future__ import annotations

import base64
import json
import shutil

import pytest
import yaml

from lockstep.checks import doctor
from lockstep.emit import compile_spec
from lockstep.lifecycle import FETCH_TOKEN_ENV, _auth_args, fetch
from lockstep.spec.load import load_manifest_only, load_spec

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"
CI = ".github/workflows/pipeline-ci.yml"


@pytest.fixture
def consumer(tmp_path):
    for name in ("upstream-standards", "upstream-review", "consumer"):
        shutil.copytree(FIXTURES / name, tmp_path / name)
    root = tmp_path / "consumer"
    fetch(load_manifest_only(root), root)
    return root


def declare(root, block):
    manifest = root / "pipeline.yaml"
    manifest.write_text(manifest.read_text() + block, encoding="utf-8")


def pin_app_action(root):
    path = root / ".pipeline/pins.lock"
    pins = json.loads(path.read_text())
    pins["external"]["actions/create-github-app-token"] = {"sha": "a" * 40, "tag": "v2"}
    path.write_text(json.dumps(pins), encoding="utf-8")


def fetch_steps(root):
    ci = yaml.safe_load(compile_spec(root).files[CI])
    return ci["jobs"]["drift"]["steps"]


# --- the default: nothing, because nothing is needed ------------------------


def test_a_public_upstream_needs_no_credential(consumer):
    """`lockstep fetch` reads a public repository anonymously. Wiring a token in would be noise."""
    fetch_step = next(s for s in fetch_steps(consumer) if s.get("run") == "lockstep fetch")
    assert "env" not in fetch_step


def test_the_app_action_is_not_pinned_for_a_pipeline_that_never_emits_it(consumer):
    """Requiring a pin for an action no workflow references is a red gate with nothing behind it."""
    assert "actions/create-github-app-token" not in load_spec(consumer).external_actions_used()
    assert "DOC012" not in {f.code for f in doctor(load_spec(consumer), consumer).findings}


# --- a token -----------------------------------------------------------------


def test_a_declared_token_reaches_the_fetch_step(consumer):
    declare(consumer, "\ninherits-auth:\n  token: PIPELINE_FETCH_TOKEN\n")
    fetch_step = next(s for s in fetch_steps(consumer) if s.get("run") == "lockstep fetch")
    assert fetch_step["env"] == {FETCH_TOKEN_ENV: "${{ secrets.PIPELINE_FETCH_TOKEN }}"}


def test_a_token_needs_no_extra_step(consumer):
    declare(consumer, "\ninherits-auth:\n  token: PIPELINE_FETCH_TOKEN\n")
    assert not [s for s in fetch_steps(consumer) if s.get("id") == "inherits-token"]


# --- an App ------------------------------------------------------------------


def test_an_app_mints_a_token_before_fetching(consumer):
    declare(consumer, "\ninherits-auth:\n  app-id: APP_ID\n  private-key: APP_KEY\n")
    pin_app_action(consumer)
    steps = fetch_steps(consumer)
    mint = next(s for s in steps if s.get("id") == "inherits-token")
    fetch_step = next(s for s in steps if s.get("run") == "lockstep fetch")

    assert mint["with"]["app-id"] == "${{ vars.APP_ID }}"
    assert mint["with"]["private-key"] == "${{ secrets.APP_KEY }}"
    assert fetch_step["env"] == {FETCH_TOKEN_ENV: "${{ steps.inherits-token.outputs.token }}"}
    assert steps.index(mint) < steps.index(fetch_step)


def test_the_app_action_is_pinned_like_any_other(consumer):
    declare(consumer, "\ninherits-auth:\n  app-id: APP_ID\n  private-key: APP_KEY\n")
    pin_app_action(consumer)
    mint = next(s for s in fetch_steps(consumer) if s.get("id") == "inherits-token")
    assert mint["uses"] == "actions/create-github-app-token@" + "a" * 40


def test_an_unpinned_app_action_refuses_to_compile(consumer):
    """The same rule every third-party action follows; declaring one does not exempt it."""
    from lockstep.errors import EmitError

    declare(consumer, "\ninherits-auth:\n  app-id: APP_ID\n  private-key: APP_KEY\n")
    with pytest.raises(EmitError) as error:
        compile_spec(consumer)
    assert "create-github-app-token" in error.value.message


def test_half_an_app_declaration_is_not_an_app(consumer):
    """An id with no key cannot mint anything; silently emitting a broken step would be worse."""
    declare(consumer, "\ninherits-auth:\n  app-id: APP_ID\n")
    assert not [s for s in fetch_steps(consumer) if s.get("id") == "inherits-token"]


# --- the credential itself ---------------------------------------------------


def test_the_token_never_reaches_the_remote_url():
    """A URL with a token in it is the form that ends up quoted back in error messages."""
    args = _auth_args("ghs_secret")
    assert "ghs_secret" not in " ".join(args)
    assert args[0] == "-c"
    assert base64.b64encode(b"x-access-token:ghs_secret").decode() in args[1]


def test_no_credential_means_no_header():
    assert _auth_args("") == []


def test_a_failed_fetch_does_not_echo_the_credential(tmp_path, monkeypatch):
    """git can quote the URL back; a leaked token in a build log is a leaked token."""
    from lockstep.lifecycle import PinError, _clone_at

    monkeypatch.setenv(FETCH_TOKEN_ENV, "ghs_secret")
    with pytest.raises(PinError) as error:
        _clone_at("acme/nope", "0" * 40, tmp_path / "dest", token="ghs_secret")
    assert "ghs_secret" not in error.value.render()


def test_the_unauthenticated_failure_says_what_to_declare(tmp_path):
    from lockstep.lifecycle import PinError, _clone_at

    with pytest.raises(PinError) as error:
        _clone_at("acme/nope", "0" * 40, tmp_path / "dest")
    assert "inherits-auth" in error.value.hint
