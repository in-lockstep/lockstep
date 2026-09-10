"""Where a model's staged work executes, resolved once and used by everything that asks.

The rule was asked of the bound adapter's `sandbox=`, which forced one binding to carry a property
only one of its callers needs. `selfcheck` validates the working tree -- a person's code, on the
host, where this repository's `make lint` works -- and the same binding had to be the container a
staged change requires. Three attempts each satisfied one caller by breaking the other: contain it
and `selfcheck` deleted its own venv (#418), leave it on the host and the model path was refused
(#420), give `Provision` an image and the CI provision step containerised (#424).

`staged_runner` is that distinction: the binding's image where it names one, the workshop's
otherwise. And it is ONE function because the refusal asks about a runner and the dispatch has to
use it -- a control that inspected one while another executed would be the defect wearing the
control's own clothes, which this repository shipped twice in a week (#412, #421).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from in_lockstep.adapters.command import CommandValidate
from in_lockstep.adapters.sandbox import Runner, Sandbox, SandboxResult
from in_lockstep.adapters.worktree import staged_refusal, staged_runner
from in_lockstep.core.types import Validate
from in_lockstep.core.verbs import Capability


def _ctx(*, binding: Any, workshop: Any = None) -> Any:
    class _Container:
        @staticmethod
        def has(verb: type) -> bool:
            return verb is Validate and binding is not None

        @staticmethod
        def resolve(verb: type) -> Any:
            return binding

    return SimpleNamespace(container=_Container(), workshop_runner=workshop)


def _adapter(sandbox: Any) -> Any:
    return SimpleNamespace(sandbox=sandbox, capabilities=frozenset({Capability.EXECUTES_CODE}))


def test_a_binding_that_names_an_image_wins() -> None:
    """An adopter may say where a particular verb runs. It is an override, not the only answer."""
    own = Sandbox(image="ghcr.io/example/lint:pinned")
    ctx = _ctx(binding=_adapter(own), workshop=Sandbox(image="other"))
    assert staged_runner(ctx, Validate) is own


def test_a_binding_that_names_none_gets_the_workshop() -> None:
    """Which is what lets `Validate` stay bound to the host for `selfcheck` and still be contained
    for a staged change. One `sandbox=` could never be both."""
    shop = Sandbox(image="ghcr.io/example/work:pinned")
    ctx = _ctx(binding=_adapter(Sandbox()), workshop=shop)
    assert staged_runner(ctx, Validate) is shop


def test_the_workshop_runner_is_unwrapped() -> None:
    """`Lockstep.use` wraps it in a `WorktreeRunner`, whose job is a throwaway worktree of HEAD --
    right for `run_script`, wrong here. `prepared` has already built the tree with the change in
    it, and a second worktree of HEAD would check the code as it was before."""
    inner = Sandbox(image="ghcr.io/example/work:pinned")
    ctx = _ctx(binding=_adapter(Sandbox()), workshop=SimpleNamespace(inner=inner))
    assert staged_runner(ctx, Validate) is inner


def test_gate_sandbox_2_the_refusal_asks_about_the_runner_the_dispatch_uses() -> None:
    """GATE-SANDBOX-2. The property that keeps this a control rather than a decoration: both go
    through `staged_runner`, so they cannot resolve differently."""
    shop = Sandbox(image="ghcr.io/example/work:pinned", require_container=True)
    ctx = _ctx(binding=_adapter(Sandbox()), workshop=shop)

    assert staged_refusal(ctx, Validate) is None, "a contained workshop must clear the refusal"
    assert staged_runner(ctx, Validate) is shop, "and it is that runner the dispatch is handed"


def test_gate_sandbox_2_an_uncontained_staged_runner_is_refused_by_name() -> None:
    """GATE-SANDBOX-2. The other direction, so the test above is not passing because the refusal
    never fires."""
    ctx = _ctx(binding=_adapter(Sandbox()), workshop=Sandbox())
    why = staged_refusal(ctx, Validate)
    assert why is not None and "container image" in why


def test_the_adapter_runs_where_the_request_says() -> None:
    """`runner` is the same contract `root` has, on the other axis: the caller that knows supplies
    it, and an adapter that reads it honours it. Without this the refusal would clear a runner that
    nothing then used."""
    ran: list[tuple[str, list[str]]] = []

    class _Runner(Runner):
        def __init__(self, name: str) -> None:
            self.name = name
            self.image = f"img/{name}"

        async def run(self, command: list[str], *, cwd: str | None = None, timeout: float = 900.0) -> Any:
            ran.append((self.name, command))
            return SandboxResult(exit_code=0, stdout="", stderr="", sandboxed=True, how="container")

    adapter = CommandValidate(["make", "lint"], sandbox=_Runner("binding"))
    handed = _Runner("handed")
    asyncio.run(adapter.invoke(SimpleNamespace(repo=SimpleNamespace(root=".")), Validate(runner=handed)))

    assert [who for who, _cmd in ran] == ["handed"], "the adapter ignored the runner it was given"


def test_the_adapter_runs_where_its_binding_says_when_nobody_supplies_one() -> None:
    """Every path a person drives: `selfcheck`, and a laptop. Absent means the binding's own."""
    ran: list[str] = []

    class _Runner(Runner):
        image = ""

        async def run(self, command: list[str], *, cwd: str | None = None, timeout: float = 900.0) -> Any:
            ran.append("binding")
            return SandboxResult(exit_code=0, stdout="", stderr="", sandboxed=False, how="subprocess")

    adapter = CommandValidate(["make", "lint"], sandbox=_Runner())
    asyncio.run(adapter.invoke(SimpleNamespace(repo=SimpleNamespace(root=".")), Validate()))
    assert ran == ["binding"]
