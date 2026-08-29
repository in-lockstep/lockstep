"""Running things out of process.

Two reasons, and the second is the one that is easy to miss.

Generated code should not execute on the host with ambient credentials — that much is obvious.
Less obvious: neither should the repository's own test suite. pytest collects and executes
`conftest.py` from every directory on the rootdir path, and ruff loads repository configuration.
So once a workflow composes an AI verb with a test run, in-process execution hands
repository-authored — and later, agent-authored — Python direct access to the live credential
objects in that process, defeating redaction, egress and the spend ceiling at once.

The compiler-era sandbox got this right and said so: restricting what a model reaches but not what
the scripts do is the wrong way round.

Container execution is preferred and a no-credential subprocess is the fallback. The fallback is
weaker and says so rather than pretending; what it does guarantee is that the child process cannot
read a credential out of the parent's memory, which is the specific hole this closes.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass, field

# Passed through to a sandboxed child. Everything else — every credential — is dropped.
SAFE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "PYTHONPATH", "CI")

# The floor the compiler applied to every executor job, kept.
DOCKER_FLAGS = (
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "--network=none",
)


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    sandboxed: bool
    how: str


@dataclass
class Sandbox:
    """Executes a command with no ambient credentials."""

    image: str = ""
    allow_network: bool = False
    memory: str = "2g"
    extra_env: dict[str, str] = field(default_factory=dict)

    def clean_env(self) -> dict[str, str]:
        env = {k: os.environ[k] for k in SAFE_ENV if k in os.environ}
        env.update(self.extra_env)
        return env

    async def run(
        self, command: list[str], *, cwd: str | None = None, timeout: float = 900.0
    ) -> SandboxResult:
        if self.image and shutil.which("docker"):
            return await self._docker(command, cwd=cwd, timeout=timeout)
        return await self._subprocess(command, cwd=cwd, timeout=timeout)

    async def _docker(self, command: list[str], *, cwd: str | None, timeout: float) -> SandboxResult:
        mount = cwd or os.getcwd()
        flags = [f for f in DOCKER_FLAGS if not (f == "--network=none" and self.allow_network)]
        argv = [
            "docker",
            "run",
            "--rm",
            *flags,
            f"--memory={self.memory}",
            "-v",
            f"{mount}:/work",
            "-w",
            "/work",
            self.image,
            *command,
        ]
        code, out, err = await _exec(argv, cwd=mount, env=self.clean_env(), timeout=timeout)
        return SandboxResult(code, out, err, sandboxed=True, how=f"docker:{self.image}")

    async def _subprocess(self, command: list[str], *, cwd: str | None, timeout: float) -> SandboxResult:
        code, out, err = await _exec(command, cwd=cwd, env=self.clean_env(), timeout=timeout)
        # Honest about what this is: a separate process with no inherited credentials. It is not
        # a kernel sandbox, and calling it one would be the sort of claim this codebase avoids.
        return SandboxResult(code, out, err, sandboxed=False, how="subprocess:no-credentials")


class UnsandboxedRun:
    """The opt-out, named after what it does.

    Binding this is a deliberate, greppable, reviewable act — which is the whole point of making
    the unsafe path require a differently-named adapter rather than a flag.
    """

    async def run(
        self, command: list[str], *, cwd: str | None = None, timeout: float = 900.0
    ) -> SandboxResult:
        code, out, err = await _exec(command, cwd=cwd, env=dict(os.environ), timeout=timeout)
        return SandboxResult(code, out, err, sandboxed=False, how="unsandboxed")


async def _exec(
    argv: list[str], *, cwd: str | None, env: dict[str, str], timeout: float
) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        return 127, "", str(e)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return 124, "", f"timed out after {timeout}s"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")
