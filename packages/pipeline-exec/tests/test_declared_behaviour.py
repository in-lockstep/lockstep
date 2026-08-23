"""Behaviour that used to be one application's, now declared by the pipeline.

The code these replace could not be tested without that application in front of it — which is how a
login recipe for one product survived inside a general runtime. Declared, both are ordinary data.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from pipeline_exec.errors import ExecError
from pipeline_exec.executors import login as login_module
from pipeline_exec.executors.login import LoginRecipe, looks_expired, sign_in
from pipeline_exec.executors.recovery import Recovery


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """The algorithm is what is under test; the pauses are for a real browser."""
    monkeypatch.setattr(login_module, "RETRY_DELAY", 0)
    monkeypatch.setattr(login_module, "SETTLE_DELAY", 0)


@dataclass
class Result:
    text: str = "ok"


class FakeBrowser:
    """Records what a recipe drove, and answers whatever the test lined up."""

    def __init__(self, page: str = "", failing: tuple[str, ...] = ()):
        self.calls: list[tuple[str, dict]] = []
        self.page = page
        self.failing = failing

    async def execute_tool(self, tool, params):
        self.calls.append((tool, params))
        if params.get("selector") in self.failing or params.get("text") in self.failing:
            return Result("Failed: no matching element")
        if tool == "get_page_snapshot":
            return Result(self.page)
        return Result()

    def filled(self, value):
        return [p["selector"] for t, p in self.calls if t == "fill" and p.get("value") == value]


def recipe(**overrides):
    base = {
        "username_selectors": ["#user"],
        "password_selectors": ["#pass"],
        "submit_selectors": ["button[type=submit]"],
    }
    return LoginRecipe(**{**base, **overrides})


def write(tmp_path, body):
    path = tmp_path / "login.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# --- the recipe ------------------------------------------------------------


def test_a_recipe_must_say_how_to_sign_in(tmp_path):
    with pytest.raises(ExecError) as excinfo:
        LoginRecipe.load(write(tmp_path, "username_selectors: ['#user']\n"))
    assert "password_selectors" in str(excinfo.value)
    assert "submit_selectors" in str(excinfo.value)


def test_a_recipe_loads_what_it_declares(tmp_path):
    loaded = LoginRecipe.load(
        write(
            tmp_path,
            "username_selectors: ['#user']\npassword_selectors: ['#pass']\n"
            "submit_selectors: ['#go']\nexpired_markers: ['Session expired']\n",
        )
    )
    assert loaded.expired_markers == ["Session expired"]


# --- signing in ------------------------------------------------------------


def test_the_declared_selectors_are_the_ones_driven():
    browser = FakeBrowser()
    assert asyncio.run(sign_in(browser, recipe(), url="https://app.test", username="u", password="p"))
    assert browser.filled("u") == ["#user"]
    assert browser.filled("p") == ["#pass"]
    assert ("click", {"selector": "button[type=submit]"}) in browser.calls


def test_selectors_are_tried_in_order_until_one_works():
    browser = FakeBrowser(failing=("#first",))
    asyncio.run(
        sign_in(
            browser,
            recipe(username_selectors=["#first", "#second"]),
            url="https://app.test",
            username="u",
            password="p",
        )
    )
    assert browser.filled("u") == ["#first", "#second"]


def test_a_session_already_signed_in_is_left_alone():
    browser = FakeBrowser(page="Welcome back, this page is long enough to count as loaded content.")
    assert asyncio.run(
        sign_in(
            browser,
            recipe(signed_out_markers=["Sign in"]),
            url="https://app.test",
            username="u",
            password="p",
        )
    )
    assert not browser.filled("u")


def test_a_provider_link_is_chosen_before_the_form():
    browser = FakeBrowser()
    asyncio.run(
        sign_in(
            browser,
            recipe(provider_links=["Use a local account"]),
            url="https://app.test",
            username="u",
            password="p",
        )
    )
    tools = [tool for tool, _ in browser.calls]
    assert tools.index("click_text") < tools.index("fill")


def test_a_page_that_never_accepts_the_password_reports_failure():
    browser = FakeBrowser(failing=("#pass",))
    assert not asyncio.run(
        sign_in(browser, recipe(), url="https://app.test", username="u", password="p")
    )


def test_expiry_is_only_detected_when_the_pipeline_says_what_it_looks_like():
    assert not looks_expired(recipe(), "Please log in")
    assert looks_expired(recipe(expired_markers=["Please log in"]), "Please log in")


# --- 422 recovery ----------------------------------------------------------


def test_recovery_without_values_is_refused(tmp_path):
    path = tmp_path / "recovery.yaml"
    path.write_text("pattern: 'x'\n", encoding="utf-8")
    with pytest.raises(ExecError) as excinfo:
        Recovery.load(path)
    assert "nothing to retry" in str(excinfo.value)


def test_only_declared_fields_are_supplied():
    rules = Recovery(defaults={"scope": "team"})
    body = {}
    assert rules.patch(body, "scope: Field required\nsecret_key: Field required", {})
    assert body == {"scope": "team"}


def test_a_field_the_request_already_has_is_left_as_it_is():
    rules = Recovery(defaults={"scope": "team"})
    body = {"scope": "mine"}
    assert not rules.patch(body, "scope: Field required", {})
    assert body["scope"] == "mine"


def test_a_declared_value_may_come_from_a_runtime_variable():
    rules = Recovery(defaults={"project_id": "{PROJECT_ID}"})
    body = {}
    rules.patch(body, "project_id: Field required", {"PROJECT_ID": "p-42"})
    assert body["project_id"] == "p-42"


def test_a_declared_value_may_be_unique_per_attempt():
    rules = Recovery(defaults={"name": "{random}"})
    first, second = {}, {}
    rules.patch(first, "name: Field required", {})
    rules.patch(second, "name: Field required", {})
    assert first["name"] != second["name"]


def test_the_message_format_is_overridable():
    rules = Recovery(defaults={"scope": "team"}, pattern=r"missing field '(\w+)'")
    body = {}
    assert rules.patch(body, "missing field 'scope'", {})
    assert body == {"scope": "team"}
