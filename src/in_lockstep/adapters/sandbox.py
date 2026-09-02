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
import signal
from dataclasses import dataclass, field

# Passed through to a sandboxed child. Everything else — every credential — is dropped.
SAFE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "PYTHONPATH", "CI")

# Runtimes that take these flags, in preference order. Podman is here because it is the drop-in on
# macOS and Fedora and accepts `--cap-drop`, `--security-opt` and `--network` identically — and
# because a `docker` shell alias pointing at it is invisible from here: `shutil.which` looks for a
# binary and `create_subprocess_exec` never runs a shell. So a machine with a perfectly good
# container runtime was silently falling through to the weaker path, which is the failure mode
# worth avoiding in a security control: not refusing, but quietly doing less.
CONTAINER_RUNTIMES = ("docker", "podman")

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
    #: Variables the command sees beside the pass-through set: joined to it for a subprocess,
    #: forwarded as `-e` flags into a container. `CommandRun` fills this from `Run.env`.
    extra_env: dict[str, str] = field(default_factory=dict)
    #: Refuse rather than fall back to a bare subprocess when no container runtime is available.
    #:
    #: The fallback is the right default for the repository's own test suite: that code is already
    #: trusted enough to be in the repository, and dropping credentials is the whole win. It is
    #: the wrong default for a command a MODEL chose, on a host whose egress is unconstrained —
    #: there the fallback quietly removes the only thing standing between an injected ticket and
    #: an outbound connection. So the caller says which situation it is in, and gets a refusal
    #: instead of a weaker guarantee it did not ask for.
    require_container: bool = False

    def clean_env(self) -> dict[str, str]:
        """What a subprocess sees: the pass-through set plus `extra_env`. Not what a container
        runtime's client sees; `_container` keeps `extra_env` off the client on purpose."""
        env = self._passthrough()
        env.update(self.extra_env)
        return env

    def _passthrough(self) -> dict[str, str]:
        return {k: os.environ[k] for k in SAFE_ENV if k in os.environ}

    def runtime(self) -> str | None:
        """The container runtime available here, or None. Named so `doctor` can ask."""
        for candidate in CONTAINER_RUNTIMES:
            found = shutil.which(candidate)
            if found:
                return found
        return None

    async def run(
        self, command: list[str], *, cwd: str | None = None, timeout: float = 900.0
    ) -> SandboxResult:
        runtime = self.runtime() if self.image else None
        if runtime:
            return await self._container(runtime, command, cwd=cwd, timeout=timeout)
        if self.require_container:
            why = (
                "no image was named"
                if not self.image
                else f"neither {' nor '.join(CONTAINER_RUNTIMES)} is on PATH"
            )
            return SandboxResult(
                exit_code=126,
                stdout="",
                stderr=(
                    f"refusing to run outside a container: {why}. This runner was constructed to "
                    f"require one, so it will not fall back to a subprocess on the host."
                ),
                sandboxed=False,
                how="refused:no-container",
            )
        return await self._subprocess(command, cwd=cwd, timeout=timeout)

    async def _container(
        self, runtime: str, command: list[str], *, cwd: str | None, timeout: float
    ) -> SandboxResult:
        # Absolute, because a bind mount source must be. A relative `cwd` reached the runtime as
        # `-v .:/work`, which mounts something that is not the working tree and produces a
        # "file not found" for a file that is plainly there — a confusing failure a long way from
        # its cause.
        mount = os.path.abspath(cwd or os.getcwd())
        flags = [f for f in DOCKER_FLAGS if not (f == "--network=none" and self.allow_network)]
        # `extra_env` reaches the command as `-e` flags and reaches the runtime's client not at
        # all. A container inherits nothing from the client's environment, so putting the
        # variables there (which is what this did before anything populated `extra_env`) meant a
        # `Run.env` was honoured by the subprocess fallback and silently dropped by the stronger
        # path. And the client is the wrong place for a request's variables for a second reason:
        # `DOCKER_HOST` or `CONTAINER_HOST` there points the whole "sandboxed" run at another
        # daemon. The client gets the same pass-through set a subprocess would, and nothing else.
        env_flags = [item for key, value in self.extra_env.items() for item in ("-e", f"{key}={value}")]
        argv = [
            runtime,
            "run",
            "--rm",
            *flags,
            f"--memory={self.memory}",
            *env_flags,
            "-v",
            f"{mount}:/work",
            "-w",
            "/work",
            self.image,
            *command,
        ]
        code, out, err = await _exec(argv, cwd=mount, env=self._passthrough(), timeout=timeout)
        # The runtime is named in `how` rather than hardcoded as "docker": a result that says
        # which thing ran it is the difference between reading a transcript and guessing at one.
        name = os.path.basename(runtime)
        return SandboxResult(code, out, err, sandboxed=True, how=f"{name}:{self.image}")

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
        # Its own session, so that a timeout can kill everything the command started and not only
        # the command. `npm start` is `sh -c "node server.js"`: killing npm alone left the server
        # bound to its port after the run had reported 124, and the next run on the same host met
        # it. The cost is that a terminal's Ctrl-C no longer reaches the child directly, which is
        # why cancellation below kills the group too.
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as e:
        return 127, "", str(e)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        _kill_group(proc)
        await proc.wait()
        return 124, "", f"timed out after {timeout}s"
    except asyncio.CancelledError:
        _kill_group(proc)
        raise
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the child's whole process group; the child alone when there is no such thing."""
    killpg = getattr(os, "killpg", None)
    try:
        if killpg is not None:
            # The child is a session leader (`start_new_session`), so its group id is its pid.
            killpg(proc.pid, signal.SIGKILL)
        else:  # pragma: no cover - not a platform this runs on
            proc.kill()
    except ProcessLookupError:
        pass
